"""S13 (MHCLG Local Authority Housing Statistics, households on the housing
register): edition history and loader on the period-editions engine.

The live table la_housing_register keeps its name, key (lad24cd,
reporting_year) and columns and is the latest-edition layer (W1 and the map
read households_on_register at the newest reporting_year). This module owns
its editions table and file-check ledger, append-only:

    SPEC     la_housing_register_editions             period reporting_year
    ledger   la_housing_register_editions_file_checks

An edition is what one LAHS open data file says about one reporting year
(the LAHS year yyyy-yy ends on 31 March of the second year: 2024-25 is
reporting_year 2025). Values: households_on_register (LAHS cc1a),
jointly_managed_register (the live column's name, kept; it holds cc2a, which
the LAHS data dictionary defines as "Have you changed your housing register
(or waiting list) criteria since last year in light of the changes in the
Localism Act 2011?"; it is not a jointly-managed flag), reasonable_preference
(cc5a). Editions-only columns: value_flag (why a value is NULL, one
'column=reason' per NULL value, '; '-joined; NULL when no value is NULL),
return_status (the CSV's status; several predecessors joined with ';' in the
order of predecessor_codes), imputed_cc1a (true/false from the year ODS's
Imputations sheet where read; NULL = not checked), predecessor_codes (the
publisher codes the record was built from, sorted, ';'-joined) and
live_source (the text refresh-latest copies into the live source column: the
edition's release label plus any rule note; not compared, not in the content
sha).

Source and identity (rule 3). The data is the LAHS open data CSV
('Local Authority Housing Statistics open data 1978-79 to <yyyy-yy>' on the
GOV.UK page 'Local Authority Housing Statistics open data'), which restates
every year and is revised in place (the newest year each February, the June
revisions). It carries no title or date of its own, so its identity is the
newest year's accessible ODS ('Local Authority Housing Statistics regional
and local authority data <yyyy> to <yyyy>' on the page 'Local Authority
Housing Statistics data returns for <yyyy> to <yyyy>' of the collection
'Local authority housing data'): its Cover title 'Local Authority Housing
Statistics (LAHS) Data: <yyyy-yy>' must be the CSV's newest year, its
symbols line must define [x], [z] and [s], its 'Latest update' date is the
rank of the pair (never a file name, link or media id), and the CSV's newest
year must equal the ODS's Local_Authority_Data on cc1a, cc2a and cc5a for
every authority (cross_check). The ODS's Data_Dictionary definitions of the
variables read must equal the surveyed text (a change of meaning halts).
The 2024-25 ODS has no cc2a column and no cc2a definition (read
2026-10-10): then every cc2a cell of the CSV's newest year must be [z] (it
is, as published), else the cross-check halts.

History (so the wording here stays truthful): the n8n workflow "Social
housing waiting lists (S13)" loaded the table on 2026-04-01 (pipeline_run_log
28) from a CSV converted in a chat session from the February 2026 open data
file (data/reference/LAHS_open_data_1978-79_to_2024-25.csv, sha256
addaa0035e2fdc97...). Its insert joined la_code_lookup and kept one row per
(new code, year) with DISTINCT ON, so for the 11 authorities created by
reorganisation (BCP, Dorset, Buckinghamshire, North and West
Northamptonshire, Cumberland, Westmorland and Furness, North Yorkshire,
Somerset, East and West Suffolk) one arbitrary predecessor's figure survived
for 2015 to 2023 (76 keys), not the sum. cc5a was never loaded. On
2026-08-20 three 2025 rows (Bromley, Hillingdon, Mansfield) were set by
direct SQL to the 25 June 2026 revision, and on 2026-09-30 Telford and
Wrekin's 2022, 2023 and 2025 zeros were set NULL by direct SQL (decision
note 2026-09-30). migrate-legacy (one-off) records every held year as
edition 1 "as loaded" (live as held, always), with a proof that the n8n
rules on the February file reproduce it (and the June file the three later
rows); the first load of the June file then stores edition 2 of each year it
changes, released only by the named correction below.

Blanks and zeros (docs/RULES.md rule 1). The publisher's markers [x] (not
available), [z] (not applicable) and [s] (suppressed due to data quality
concerns) are NULL with the reason in value_flag; any other non-number (a
blank, '-', 'x', 'n/a', a negative, a decimal) halts (cell_int,
cell_yes_no). A published 0 stays 0, except under exactly two parser rules,
each a module constant with its evidence and listed in every preview
(ZERO_RULES): TELFORD_NO_REGISTER and ALLERDALE_ZERO. A successor's total is
NULL ('part_missing') unless every predecessor has a figure (rule 1.7); its
cc2a is taken only when every predecessor agrees ('parts_disagree'
otherwise). Predecessor sums for the 11 reorganised councils are a
documented transformation rule (Scott).

Geography (rule 4): rows are keyed on the publisher's local_authority_code
(declared 'old': Barnsley and Sheffield as E08000016/19), resolved by
geography.resolve('13', ...), then la_code_lookup rows of type new_unitary
or merger with exactly one target in la_boundaries, then LOOKUP_GAPS (the five
Dorset district codes la_code_lookup lacks, each with its evidence). The CSV's
own LAD24CD must equal the resolved code (or be E08000038/39 where the
resolved code is E08000016/19); anything else is UNEXPLAINED and stops.

Stop conditions (calibrated 2026-10-10 on the June 2026 file's 2015-2025
series as this loader builds it, read-only; CALIBRATION below has the
per-year figures): the England total of households_on_register moved
between consecutive years by -5.6% (2015 to 2016) to +6.0% (2022 to 2023),
and 11 to 28 authorities a year moved by more than 50% (28 in 2022, the
most). Between the February and June 2026 files the only revised year was
2025: 6 authorities changed and the England total moved by +0.007%:

    new year:      the England total (over authorities with a figure in both
                   years) moving more than NEW_TOTAL_PCT against the latest
                   earlier held year, or more than NEW_AREAS_MOVING
                   authorities moving by more than NEW_AREA_PCT
    revised year:  the England total moving more than REV_TOTAL_PCT, more
                   than REV_MAX_AREAS authorities changing, or any value going
                   to or from NULL; a value going from 0 to NULL or NULL to 0
                   (rule 1.10) is released only by a named correction
    both:          fewer authorities than EXPECTED_AREAS or than the held
                   edition: a partial file never replaces a fuller edition,
                   and no flag releases that

A stopped year is REJECTED (nothing stored, no ledger row, exit 1);
--acknowledge YEAR releases a year's thresholds (never a partial file, never
a 0/NULL change); --acknowledge-correction NAME releases exactly the changes
ACKNOWLEDGED_CORRECTIONS[NAME] names for each year it lists (counts per
group, nothing else). Equal rank with different content stops unless
--accept-reissue YEAR. Each is written to the edition's label and the run
log.

W1 input: a new year's live rows take loaded_at = now(); a revision reaches
live only through refresh-latest, which copies the edition's loaded_at, so
run refresh-latest in the same session before W1.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
years.
    python scripts/s13_lahs_editions.py ddl [--commit | --simulate]
    python scripts/s13_lahs_editions.py status
    python scripts/s13_lahs_editions.py load [--release YYYY-YY]
        [--file PATH [--ods PATH] [--no-page]] [--recheck YYYY]
        [--allow-older-file] [--acknowledge YYYY ...]
        [--accept-reissue YYYY ...] [--acknowledge-correction NAME]
        [--commit | --simulate]
        # the CSV and the ODS are downloaded to data/raw/s13_lahs/ (also in
        # a preview; a preview writes nothing to the database)
    python scripts/s13_lahs_editions.py refresh-latest [--commit | --simulate]
    python scripts/s13_lahs_editions.py migrate-legacy FILE
        [--commit | --simulate]
    python scripts/s13_lahs_editions.py restore-edition YYYY N
        [--commit | --simulate]
"""
import argparse
import csv
import dataclasses
import hashlib
import json
import math
import numbers
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s13_lahs_editions"
RUN_SOURCE = "13"
LIVE_MISSING = pe.LIVE_MISSING

API = "https://www.gov.uk/api/content"
GOV_UK = "https://www.gov.uk"
OPEN_DATA_PATH = ("/government/statistical-data-sets/local-authority-housing-"
                  "statistics-open-data")
OPEN_DATA_TITLE = "Local Authority Housing Statistics open data"
COLLECTION_PATH = "/government/collections/local-authority-housing-data"
COLLECTION_TITLE = "Local authority housing data"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s13_lahs"               # git-ignored
USER_AGENT = ("ucws-pipeline S13 loader (read-only download of the public "
              "MHCLG Local Authority Housing Statistics files)")
MIN_FILE_BYTES = 100 * 1024
ODS_MIME = b"application/vnd.oasis.opendocument.spreadsheet"
NO_PAGE = "--file --no-page: the year page was not read"

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-10:
# lad24cd character varying NOT NULL, reporting_year integer NOT NULL,
# households_on_register integer, jointly_managed_register boolean,
# reasonable_preference integer, source text, loaded_at timestamptz DEFAULT
# now(); PK (lad24cd, reporting_year)). All three values are nullable: a
# NULL carries its reason in value_flag.
# ---------------------------------------------------------------------------

VALUE_TYPES = (("households_on_register", "integer"),
               ("jointly_managed_register", "boolean"),
               ("reasonable_preference", "integer"))
VALUES = tuple(c for c, _ in VALUE_TYPES)
EXTRA_TYPES = (("value_flag", "text"), ("return_status", "text"),
               ("imputed_cc1a", "boolean"), ("predecessor_codes", "text"))
EXTRAS = tuple(c for c, _ in EXTRA_TYPES)
PROVENANCE_TYPES = (("live_source", "text"),)
CONTENT = VALUES + EXTRAS
KEY = "lad24cd"
PERIOD = "reporting_year"
HOUSEHOLDS, JOINTLY, REASONABLE = VALUES
VARS = {"cc1a": HOUSEHOLDS, "cc2a": JOINTLY, "cc5a": REASONABLE}

# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------

FIRST_YEAR = "2014-15"            # the first LAHS year read (reporting 2015)
LABEL_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
OPEN_DATA_RE = re.compile(r"Local Authority Housing Statistics open data "
                          r"1978-79 to ([0-9]{4}-[0-9]{2})")
YEAR_DOC_RE = re.compile(r"Local Authority Housing Statistics data returns "
                         r"for ([0-9]{4}) to ([0-9]{4})")
# a title that looks like a year page however it is cased or worded
YEAR_SERIES_RE = re.compile(r"local authority housing statistics.*data "
                            r"returns", re.I)
YEAR_ODS_RE = re.compile(r"Local Authority Housing Statistics regional and "
                         r"local authority data ([0-9]{4}) to ([0-9]{4})")
COVER_TITLE_RE = re.compile(r"Local Authority Housing Statistics \(LAHS\) "
                            r"Data: ([0-9]{4}-[0-9]{2})")
SYMBOL_RE = re.compile(r"\[([a-z])\] = ([^,\[]+)")
MARKER_LETTERS = ("x", "z", "s")
MARKERS = {"[x]": "not_available", "[z]": "not_applicable",
           "[s]": "suppressed"}
NUM_RE = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+")
DISTRICT_RE = re.compile(r"E0[6-9][0-9]{6}")
CSV_REQUIRED = ("local_authority", "local_authority_code", "LAD24CD", "Year",
                "status", "cc1a", "cc2a", "cc5a")
ODS_REQUIRED = ("local_authority", "local_authority_code", "status", "cc1a",
                "cc5a")
IMPUTATIONS_HEADER = ("Local authority", "Variable", "Original value",
                      "Imputed value")
# The Data_Dictionary 'Description' of each variable read, as surveyed
# 2026-10-10 in the 2023-24 and 2024-25 ODS files (the 2024-25 file has no
# cc2a). A different description is a change of meaning and halts.
DEFINITIONS = {
    "cc1a": "Households on housing register (or waiting list)",
    "cc2a": ("Have you changed your housing register (or waiting list) "
             "criteria since last year in light of the changes in the "
             "Localism Act 2011?"),
    "cc5a": ("Households on housing register (or waiting list) with "
             "reasonable preference"),
}
EXPECTED_AREAS = 296
# When the next first release is expected: data, not a claim. The 2024-25
# ODS Cover says 'Next Update November 2026 to February 2027'; from 1 March
# 2027 an open data file still ending in 2024-25 is warned about.
EXPECTED_RELEASES = {"2025-26": date(2027, 3, 1)}

# ---------------------------------------------------------------------------
# The two zero rules (the only places a published 0 becomes NULL)
# ---------------------------------------------------------------------------

TELFORD_NO_REGISTER = {
    "name": "TELFORD_NO_REGISTER", "code": "E06000020",
    "first_year": 2022, "last_year": None, "column": HOUSEHOLDS,
    "flag": "not_applicable",
    "note": ("Telford and Wrekin operates no housing register; reported 0 "
             "means not applicable. Set NULL 2026-09-30"),
    "evidence": ("docs/decisions/2026-09-30-telford-no-housing-register.md "
                 "(Scott): Telford and Wrekin does not run a housing "
                 "register; LAHS reports 0 for it from 2022. The LAHS data "
                 "dictionary's cc1a note agrees: 'From 31 March 2021 Telford "
                 "& Wrekin Council does not operate a housing housing "
                 "register (or waiting list)'."),
}
ALLERDALE_ZERO = {
    "name": "ALLERDALE_ZERO", "code": "E07000026", "successor": "E06000063",
    "first_year": 2015, "last_year": 2018, "column": HOUSEHOLDS,
    "flag": "not_counted",
    "note": ("Allerdale (a predecessor) reported 0 households 2014-15 to "
             "2017-18, read as not counted under Scott's standing ruling"),
    "evidence": ("Scott's standing ruling on a zero that may mean not counted "
                 "(blanks and zeros rule), applied 2026-10-10 and listed for "
                 "him to overrule: Allerdale reported 0 households on its "
                 "register for 2014-15 to 2017-18 and figures in the "
                 "thousands from 2018-19; Cumberland 2015-2018 is then NULL "
                 "because a part is NULL (rule 1.7)."),
}
ZERO_RULES = (TELFORD_NO_REGISTER, ALLERDALE_ZERO)

# ---------------------------------------------------------------------------
# Codes la_code_lookup lacks (read 2026-10-10): the five Dorset districts
# abolished on 1 April 2019 (Christchurch, which joined BCP, has its merger
# row). Each appears in the open data for 2014-15 to 2018-19 only;
# la_code_lookup has no row for it, so without this table it is UNEXPLAINED. The successor
# is the one the LAHS CSV's own LAD24CD column gives (February and June 2026
# files agree). Applied only while la_code_lookup has no row for the code
# (a row there makes the entry a problem: remove it), only to a target in
# la_boundaries, and only where the row's LAD24CD equals the target.
# Adding these five rows to la_code_lookup (a separate, approved change)
# retires this table.
# ---------------------------------------------------------------------------

_GAP = ("la_code_lookup has no row for it (read 2026-10-10); the LAHS open "
        "data CSV's own LAD24CD column maps it to {} for 2014-15 to 2018-19 "
        "(February and June 2026 files); {}")
LOOKUP_GAPS = {
    "E07000049": ("E06000059", _GAP.format("E06000059 Dorset", "East Dorset "
                                           "became part of Dorset Council on "
                                           "1 April 2019")),
    "E07000050": ("E06000059", _GAP.format("E06000059 Dorset", "North Dorset "
                                           "likewise")),
    "E07000051": ("E06000059", _GAP.format("E06000059 Dorset", "Purbeck "
                                           "likewise")),
    "E07000052": ("E06000059", _GAP.format("E06000059 Dorset", "West Dorset "
                                           "likewise")),
    "E07000053": ("E06000059", _GAP.format("E06000059 Dorset", "Weymouth and "
                                           "Portland likewise")),
}

# ---------------------------------------------------------------------------
# Stop conditions (see the module docstring and CALIBRATION)
# ---------------------------------------------------------------------------

NEW_TOTAL_PCT = 15          # new year: England total against the year before
NEW_AREAS_MOVING = 30       # new year: authorities moving by more than ...
NEW_AREA_PCT = 50           # ... this percentage
REV_TOTAL_PCT = 2           # revised year: England total
REV_MAX_AREAS = 20          # revised year: authorities changing
PARTIAL = "PARTIAL FILE"    # problems no flag releases

# The June 2026 file as this loader builds it, read 2026-10-10: households
# on the register, the England total over the authorities with a figure,
# its move against the year before (over the authorities with a figure in
# both years) and the authorities moving by more than 50%. Then the
# February 2026 file against the June 2026 file, both built this way.
CALIBRATION = """
2015 1,246,147 (Cumberland NULL)
2016 1,176,583  -5.6%  22 authorities moving more than 50%
2017 1,153,874  -1.9%  13
2018 1,120,822  -2.9%  12
2019 1,160,261  +2.9%  22
2020 1,137,234  -2.0%  23
2021 1,185,971  +4.3%  20
2022 1,214,724  +2.6%  28
2023 1,287,707  +6.0%  22
2024 1,330,602  +3.6%  16
2025 1,340,527  +0.6%  11
February -> June 2026: 2015-2024 unchanged; 2025 6 authorities changed
(E07000102, E07000109, E07000174, E08000005, E09000006, E09000017),
England 1,340,435 -> 1,340,527 (+0.007%).
"""

# The changes against the held edition a revised year may store under a
# named, decided correction (--acknowledge-correction NAME): per reporting
# year, exactly this many authorities in each group, and nothing outside
# the groups (classify_changes). Groups: cc5a_filled (reasonable_preference
# NULL -> the file's cc5a), predecessor_sum (households or cc2a of a
# successor built from several predecessors), allerdale (Cumberland 2015 to
# 2018 under ALLERDALE_ZERO).
ACKNOWLEDGED_CORRECTIONS = {
    "lahs-correction-2026-10": {
        "decided": ("Scott, 2026-10-10: predecessor sums for the 11 "
                    "reorganised councils are a documented transformation "
                    "rule; the Allerdale zeros are NULL under his standing "
                    "ruling, listed for him to overrule"),
        "why": ("edition 1 (as loaded) holds one predecessor's figure for "
                "the 11 reorganised authorities 2015 to 2023 (76 keys) and "
                "no cc5a; the June 2026 file read by this loader gives the "
                "successor sums (NULL where a part is missing, Cumberland "
                "2015 to 2018 under ALLERDALE_ZERO) and cc5a for every year "
                "(3,241 cells; 15 stay NULL as published [x]). No 2025 "
                "households_on_register changes"),
        # counted read-only 2026-10-10: the June 2026 file against edition 1
        # as loaded (the live table as held)
        "periods": {
            "2015": {"cc5a_filled": 296, "predecessor_sum": 10,
                     "allerdale": 1},
            "2016": {"cc5a_filled": 296, "predecessor_sum": 10,
                     "allerdale": 1},
            "2017": {"cc5a_filled": 296, "predecessor_sum": 10,
                     "allerdale": 1},
            "2018": {"cc5a_filled": 296, "predecessor_sum": 10,
                     "allerdale": 1},
            "2019": {"cc5a_filled": 296, "predecessor_sum": 11},
            "2020": {"cc5a_filled": 296, "predecessor_sum": 7},
            "2021": {"cc5a_filled": 296, "predecessor_sum": 6},
            "2022": {"cc5a_filled": 295, "predecessor_sum": 4},
            "2023": {"cc5a_filled": 292, "predecessor_sum": 4},
            "2024": {"cc5a_filled": 292},
            "2025": {"cc5a_filled": 290},
        },
    },
}

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-10
# ---------------------------------------------------------------------------

# (rows, the survey hash: md5 of lad24cd|reporting_year|households_on_register
# |jointly_managed_register|reasonable_preference|source ordered by
# reporting_year, lad24cd, see live_state; distinct loaded_at; years)
LEGACY_LIVE = (3256, "cf73fea93da41c4b" "3a0461754b27b448", 2, 11)
# the file the n8n load's CSV was made from (sha256 split so the credential
# scan does not flag it), and where it is held (the ledger's where, so a
# --file of it finds it checked)
LEGACY_FILE = {
    "name": "LAHS_open_data_1978-79_to_2024-25.csv",
    "sha256": "addaa0035e2fdc977e8ab12283014178" "51e13fd13119e69e99fdc3c42a74156d",
    "where": "data/reference/LAHS_open_data_1978-79_to_2024-25.csv",
}
# the February 2026 file's rank: the open data page's change history says
# 'Updated to include current year (2024-2025) data' on 12 February 2026.
# The February 2024-25 ODS is not held, so its own Cover date is not read.
LEGACY_RANK = date(2026, 2, 12)
# the June 2026 file, the source of the three rows set on 2026-08-20
LEGACY_JUNE = {
    "path": REPO / "data" / "raw" / "s13_lahs" /
    "LAHS_open_data_1978-79_to_2024-25.csv",
    "sha256": "6b5f0105d34488c96a8b0669113da250" "7f3c34b1e52afdbde276c2357da57fe4",
}
N8N_SOURCE = "MHCLG LAHS Section C, year ending 31 March {}"
NOT_LOADED = "not_loaded"   # value_flag of reasonable_preference in edition 1
_DEFAULT = object()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    return json.loads(x) if isinstance(x, (str, bytes)) else (x or {})


def _norm(s) -> str:
    return " ".join(str("" if s is None else s).split())


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or \
        (isinstance(v, str) and not v.strip())


def label_year(label) -> int:
    """The reporting year of a LAHS year label: '2024-25' -> 2025.
    ValueError unless it is yyyy-yy with consecutive years."""
    mm = LABEL_RE.fullmatch(str(label or ""))
    if not mm or (int(mm.group(1)) + 1) % 100 != int(mm.group(2)):
        raise ValueError(f"{label!r} is not a LAHS year yyyy-yy")
    return int(mm.group(1)) + 1


def year_label(year) -> str:
    """'2024-25' of reporting year 2025."""
    y = int(year)
    return f"{y - 1}-{y % 100:02d}"


def rank_text(rank) -> str:
    return f"latest update {rank.isoformat()}" if rank else "no file rank"


def _as_date(v) -> "date | None":
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _attachments(page_json) -> list:
    d = _json(page_json)
    return list(((d.get("details") or {}).get("attachments")) or [])


def _documents(collection_json) -> list:
    d = _json(collection_json)
    return list(((d.get("links") or {}).get("documents")) or [])


def _titles(collection_json) -> list:
    return [_norm(d.get("title")) for d in _documents(collection_json)]


def _suffix(url) -> str:
    return Path(urlparse(url or "").path).suffix.lower()


def open_data_attachment(page_json) -> dict:
    """{title, url, year, content_type, file_size, ignored} of the open data
    page's one attachment titled 'Local Authority Housing Statistics open
    data 1978-79 to <yyyy-yy>' with a .csv URL; the accessible copy
    ('... (accessible version) ...') is listed in ignored. ValueError
    (listing the attachments) when the page is not titled OPEN_DATA_TITLE or
    there is not exactly one such .csv."""
    d = _json(page_json)
    t = _norm(d.get("title"))
    if t != OPEN_DATA_TITLE:
        raise ValueError(f"the open data page is titled {t!r}, not "
                         f"{OPEN_DATA_TITLE!r}; the page has changed")
    atts = _attachments(d)
    names = [_norm(a.get("title")) for a in atts]
    hits = [a for a in atts if OPEN_DATA_RE.fullmatch(_norm(a.get("title")))]
    if len(hits) != 1 or not hits[0].get("url"):
        raise ValueError(f"{len(hits)} attachments titled 'Local Authority "
                         "Housing Statistics open data 1978-79 to <yyyy-yy>'; "
                         f"attachments: {names}")
    a = hits[0]
    if _suffix(a["url"]) != ".csv":
        raise ValueError(f"the open data attachment is not a .csv file "
                         f"({a['url']}); attachments: {names}")
    title = _norm(a["title"])
    year = OPEN_DATA_RE.fullmatch(title).group(1)
    label_year(year)
    return {"title": title, "url": a["url"], "year": year,
            "content_type": a.get("content_type"),
            "file_size": a.get("file_size"),
            "ignored": [n for n in names if n != title]}


def check_collection(collection_json) -> None:
    t = _norm(_json(collection_json).get("title"))
    if t != COLLECTION_TITLE:
        raise ValueError(f"the collection is titled {t!r}, not "
                         f"{COLLECTION_TITLE!r}; the page has changed")


def year_candidates(collection_json) -> dict:
    """{label: [(title, base_path)]} of the documents titled 'Local
    Authority Housing Statistics data returns for <yyyy> to <yyyy>'."""
    out = {}
    for doc in _documents(collection_json):
        t = _norm(doc.get("title"))
        mm = YEAR_DOC_RE.fullmatch(t)
        if mm and doc.get("base_path") and \
                int(mm.group(2)) == int(mm.group(1)) + 1:
            lb = year_label(int(mm.group(2)))
            out.setdefault(lb, []).append((t, doc["base_path"]))
    return out


def latest_year_page(collection_json) -> tuple:
    """(label, base_path) of the newest year page. ValueError (listing the
    titles seen) when the collection is retitled, none fits, two claim the
    newest year, or a title that looks like a year page but does not fit
    names a later year (never passed over for an older one)."""
    check_collection(collection_json)
    cands = year_candidates(collection_json)
    titles = _titles(collection_json)
    if not cands:
        raise ValueError("no document titled 'Local Authority Housing "
                         "Statistics data returns for <yyyy> to <yyyy>' in "
                         f"the collection; titles seen: {titles[:40]}")
    newest = max(cands)
    stray = []
    for t in titles:
        if YEAR_DOC_RE.fullmatch(t) or not YEAR_SERIES_RE.search(t):
            continue
        ys = [int(y) for y in re.findall(r"(?<![0-9])([0-9]{4})(?![0-9])", t)]
        if not ys or max(ys) > label_year(newest):
            stray.append(t)
    if stray:
        raise ValueError(f"the newest matching year page is {newest}, but the "
                         "collection lists a document that looks like a year "
                         f"page, with a later year or none, whose title does "
                         f"not fit: {stray}; not choosing an older year over "
                         f"it; titles seen: {titles[:40]}")
    if len(cands[newest]) != 1:
        raise ValueError(f"{len(cands[newest])} year pages for {newest}: "
                         f"{[t for t, _ in cands[newest]]}; refusing to "
                         "choose one")
    return newest, cands[newest][0][1]


def year_page_for(collection_json, label) -> tuple:
    """(label, base_path) of that year's page; ValueError unless exactly
    one document has its title."""
    check_collection(collection_json)
    cands = year_candidates(collection_json).get(label, [])
    if len(cands) != 1:
        raise ValueError(f"{len(cands)} documents titled 'Local Authority "
                         f"Housing Statistics data returns for "
                         f"{label_year(label) - 1} to {label_year(label)}' in "
                         f"the collection; titles seen: "
                         f"{_titles(collection_json)[:40]}")
    return label, cands[0][1]


def page_label(year_page_json) -> "str | None":
    mm = YEAR_DOC_RE.fullmatch(_norm(_json(year_page_json).get("title")))
    if not mm or int(mm.group(2)) != int(mm.group(1)) + 1:
        return None
    return year_label(int(mm.group(2)))


def year_ods_attachment(page_json) -> dict:
    """{title, url, year, ignored} of the year page's one attachment titled
    'Local Authority Housing Statistics regional and local authority data
    <yyyy> to <yyyy>' with an .ods URL; ValueError listing every attachment
    otherwise."""
    atts = _attachments(page_json)
    names = [_norm(a.get("title")) for a in atts]
    hits = [a for a in atts if YEAR_ODS_RE.fullmatch(_norm(a.get("title")))]
    if len(hits) != 1 or not hits[0].get("url") or \
            _suffix(hits[0]["url"]) != ".ods":
        raise ValueError(f"{len(hits)} attachments titled 'Local Authority "
                         "Housing Statistics regional and local authority "
                         "data <yyyy> to <yyyy>' (an .ods); attachments: "
                         f"{names}")
    a = hits[0]
    mm = YEAR_ODS_RE.fullmatch(_norm(a["title"]))
    return {"title": _norm(a["title"]), "url": a["url"],
            "year": year_label(int(mm.group(2))),
            "ignored": [n for n in names if n != _norm(a["title"])]}


def change_history(page_json) -> list:
    d = _json(page_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    return [(str(c.get("public_timestamp") or "")[:10], _norm(c.get("note")))
            for c in ch]


def _today() -> date:
    return date.today()


def discovery_note(newest_label, held_periods, today=None) -> list:
    """Lines to print when the open data file's newest year is already held:
    the newest year seen, that nothing newer is listed, and a WARNING when
    the next first release's expected date (EXPECTED_RELEASES) has passed.
    Empty when the newest year is not held."""
    if newest_label is None or str(label_year(newest_label)) not in {
            str(p) for p in held_periods}:
        return []
    out = [f"the open data file on the page runs to {newest_label}, which is "
           "already held; no newer year is listed on the page"]
    today = today or _today()
    for lb, when in sorted(EXPECTED_RELEASES.items()):
        if lb > newest_label and today >= when:
            out.append(f"WARNING: LAHS {lb} was expected by {when:%d %B %Y} "
                       "(the held ODS's 'Next Update') and today is "
                       f"{today:%d %B %Y}, but the open data file still ends "
                       f"in {newest_label}. It may be late, or discovery may "
                       "be missing it (check the GOV.UK pages by hand; --file "
                       "PATH --ods PATH loads downloaded files)")
    return out


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def cell_int(v, where) -> tuple:
    """(count, None), or (None, flag) for a publisher marker: [x]
    not_available, [z] not_applicable, [s] suppressed. A count is a
    non-negative whole number: text of digits (thousands separators
    allowed: '1,234') or a number cell equal to a whole number. Anything
    else (a blank, '-', 'x', 'n/a', a negative, a decimal, another marker)
    raises ValueError naming where. Never returns a default."""
    if isinstance(v, bool) or v is None:
        raise ValueError(f"{where}: {v!r} is not a count (a blank halts and "
                         "is never read as 0)")
    if isinstance(v, numbers.Integral):
        n = int(v)
    elif isinstance(v, numbers.Real):
        if math.isnan(v):
            raise ValueError(f"{where}: a blank cell; never read as 0")
        if not float(v).is_integer():
            raise ValueError(f"{where}: non-integer {v!r}")
        n = int(v)
    elif isinstance(v, str):
        s = v.strip()
        if s in MARKERS:
            return None, MARKERS[s]
        if not NUM_RE.fullmatch(s):
            raise ValueError(f"{where}: {v!r} is not a count or a publisher "
                             "marker ([x], [z], [s]); a blank halts and is "
                             "never read as 0")
        n = int(s.replace(",", ""))
    else:
        raise ValueError(f"{where}: {v!r} ({type(v).__name__}) is not a count")
    if n < 0:
        raise ValueError(f"{where}: negative count {n}")
    return n, None


def cell_yes_no(v, where) -> tuple:
    """(True/False, None) for 'Yes'/'No', or (None, flag) for a marker;
    anything else raises ValueError naming where."""
    if isinstance(v, str):
        s = v.strip()
        if s == "Yes":
            return True, None
        if s == "No":
            return False, None
        if s in MARKERS:
            return None, MARKERS[s]
    raise ValueError(f"{where}: {v!r} is not Yes, No or a publisher marker")


# ---------------------------------------------------------------------------
# Reading the year ODS
# ---------------------------------------------------------------------------

def _sheet_rows(book, name) -> list:
    df = book.parse(name, header=None)
    return [list(r) for r in df.itertuples(index=False, name=None)]


def _texts(rows) -> list:
    return [v for r in rows for v in r if not _blank(v)]


def _after(cells, label, where):
    """The cell after the one reading `label` (exactly one such label)."""
    idx = [i for i, c in enumerate(cells) if isinstance(c, str)
           and _norm(c).lower() == label.lower()]
    if len(idx) != 1 or idx[0] + 1 >= len(cells):
        raise ValueError(f"{where}: {len(idx)} '{label}' line(s) with a value "
                         f"after them, expected one; cells seen: "
                         f"{[_norm(c) for c in cells][:24]}")
    return cells[idx[0] + 1]


def parse_cover(rows, name="") -> dict:
    """{title, year, markers, latest_update, next_update} of the Cover.
    ValueError unless there is one 'Local Authority Housing Statistics
    (LAHS) Data: <yyyy-yy>' title, one symbols line defining [x], [z] and
    [s], a 'Latest update' line followed by a date, and a 'Next Update'
    line followed by text."""
    where = f"{name}: Cover"
    cells = _texts(rows)
    texts = [_norm(c) for c in cells if isinstance(c, str)]
    titles = [mm for mm in (COVER_TITLE_RE.fullmatch(t) for t in texts) if mm]
    if len(titles) != 1:
        raise ValueError(f"{where}: {len(titles)} titles like 'Local Authority "
                         "Housing Statistics (LAHS) Data: <yyyy-yy>'; cells "
                         f"seen: {texts[:16]}")
    year = titles[0].group(1)
    label_year(year)
    sym = [t for t in texts if "[x] =" in t or "[z] =" in t or "[s] =" in t]
    if len(sym) != 1:
        raise ValueError(f"{where}: {len(sym)} Symbols Used line(s) defining "
                         "the markers, expected one ([x], [z] and [s]); cells "
                         f"seen: {texts[:16]}")
    markers = {k: _norm(v) for k, v in SYMBOL_RE.findall(sym[0])}
    lack = [f"[{x}]" for x in MARKER_LETTERS if x not in markers]
    if lack:
        raise ValueError(f"{where}: the Symbols Used line {sym[0]!r} does not "
                         f"define {', '.join(lack)}")
    lu = _after(cells, "Latest update", where)
    latest = _as_date(lu)
    if latest is None:
        try:
            latest = datetime.strptime(_norm(lu), "%d %B %Y").date()
        except ValueError:
            raise ValueError(f"{where}: the Latest update line says "
                             f"{_norm(lu)!r}, not a date") from None
    nxt = _norm(_after(cells, "Next Update", where))
    return {"title": titles[0].group(0), "year": year, "markers": markers,
            "latest_update": latest, "next_update": nxt}


def _header_index(rows, first, where) -> int:
    for i, r in enumerate(rows[:12]):
        if r and isinstance(r[0], str) and _norm(r[0]) == first:
            return i
    raise ValueError(f"{where}: no header row starting {first!r} in the first "
                     f"12 rows; cells seen: {[_norm(c) for c in _texts(rows[:6])][:16]}")


def parse_la_data(rows, name="") -> dict:
    """{header, has_cc2a, las {code: {row, name, status, cc1a, cc2a, cc5a}},
    names {name: code}} of Local_Authority_Data (raw cells). ValueError on a
    header lacking ODS_REQUIRED (named), a row that is not an E06-E09
    authority, a repeated code or name."""
    where = f"{name}: Local_Authority_Data"
    h = _header_index(rows, "local_authority", where)
    header = [_norm(c) for c in rows[h]]
    missing = [c for c in ODS_REQUIRED if c not in header]
    if missing:
        raise ValueError(f"{where}: the header lacks {missing}; header seen: "
                         f"{header[:12]}...")
    col = {c: header.index(c) for c in ODS_REQUIRED}
    has_cc2a = "cc2a" in header
    if has_cc2a:
        col["cc2a"] = header.index("cc2a")
    las, names = {}, {}
    for n, r in enumerate(rows[h + 1:], start=h + 2):
        if all(_blank(v) for v in r):
            continue
        code = _norm(r[col["local_authority_code"]])
        nm = _norm(r[col["local_authority"]])
        if not DISTRICT_RE.fullmatch(code):
            raise ValueError(f"{where} row {n}: {code!r} ({nm}) is not an "
                             "E06-E09 authority code")
        if code in las:
            raise ValueError(f"{where} row {n}: {code} appears twice")
        if nm in names:
            raise ValueError(f"{where} row {n}: the name {nm!r} appears twice")
        las[code] = {"row": n, "name": nm,
                     "status": _norm(r[col["status"]]),
                     "cc1a": r[col["cc1a"]], "cc5a": r[col["cc5a"]],
                     "cc2a": r[col["cc2a"]] if has_cc2a else None}
        names[nm] = code
    if not las:
        raise ValueError(f"{where}: no authority rows")
    return {"header": header, "has_cc2a": has_cc2a, "las": las,
            "names": names}


def parse_imputations(rows, name="") -> list:
    where = f"{name}: Imputations"
    for i, r in enumerate(rows[:12]):
        if tuple(_norm(c) for c in r[:4]) == IMPUTATIONS_HEADER:
            return [tuple(x[:4]) for x in rows[i + 1:]
                    if not all(_blank(v) for v in x[:4])]
    raise ValueError(f"{where}: no header row {IMPUTATIONS_HEADER}; cells "
                     f"seen: {[_norm(c) for c in _texts(rows[:4])][:12]}")


def parse_dictionary(rows, name="") -> dict:
    where = f"{name}: Data_Dictionary"
    for i, r in enumerate(rows[:12]):
        if len(r) > 2 and _norm(r[0]) == "Column" and \
                _norm(r[2]) == "Description":
            return {_norm(x[0]): _norm(x[2]) for x in rows[i + 1:]
                    if len(x) > 2 and not _blank(x[0])}
    raise ValueError(f"{where}: no header row 'Column, Type, Description'")


def read_year_ods(path, year=None) -> dict:
    """The facts of one year ODS: parse_cover's (title, year, markers,
    latest_update, next_update), parse_la_data's (header, has_cc2a, las,
    names), imputations [(name, variable, original, imputed)], dictionary
    {variable: description}, path and rank (the latest update date). The
    dictionary must define cc1a and cc5a (and cc2a when the sheet has it)
    exactly as DEFINITIONS, else ValueError (a change of meaning). year
    (a yyyy-yy label, optional): the Cover's year must be it."""
    import pandas as pd
    p = Path(path)
    try:
        book = pd.ExcelFile(p, engine="odf")
    except Exception as e:  # noqa: BLE001 (not an .ods file)
        raise ValueError(f"{p.name}: not a readable .ods file ({e})") from None
    try:
        names = list(book.sheet_names)
        need = ("Cover", "Local_Authority_Data", "Imputations",
                "Data_Dictionary")
        lack = [s for s in need if s not in names]
        if lack:
            raise ValueError(f"{p.name}: no {lack} sheet(s); sheets {names}")
        out = parse_cover(_sheet_rows(book, "Cover"), p.name)
        if year is not None and out["year"] != year:
            raise ValueError(f"{p.name}: the Cover says {out['title']!r}, "
                             f"expected {year}")
        out.update(parse_la_data(_sheet_rows(book, "Local_Authority_Data"),
                                 p.name))
        out["imputations"] = parse_imputations(_sheet_rows(book,
                                                           "Imputations"),
                                               p.name)
        out["dictionary"] = parse_dictionary(_sheet_rows(book,
                                                         "Data_Dictionary"),
                                             p.name)
    finally:
        book.close()
    read = ["cc1a", "cc5a"] + (["cc2a"] if out["has_cc2a"] else [])
    for var in read:
        got = out["dictionary"].get(var)
        if got is None:
            raise ValueError(f"{p.name}: Data_Dictionary has no definition of "
                             f"{var}, which Local_Authority_Data carries")
        if got != DEFINITIONS[var]:
            raise ValueError(f"{p.name}: Data_Dictionary defines {var} as "
                             f"{got!r}, not the surveyed {DEFINITIONS[var]!r}: "
                             "a change of meaning; the reading must be "
                             "corrected deliberately, with the evidence")
    out.update(path=p, sheets=names, rank=out["latest_update"])
    return out


def imputed_flags(ods) -> set:
    """{(code, variable)} imputed by the publisher (the Imputations sheet,
    names matched exactly to Local_Authority_Data); ValueError on a name
    the sheet does not have."""
    out = set()
    for row in ods["imputations"]:
        nm, var = _norm(row[0]), _norm(row[1])
        if nm not in ods["names"]:
            raise ValueError(f"Imputations: {nm!r} is not an authority of "
                             "Local_Authority_Data")
        out.add((ods["names"][nm], var))
    return out


# ---------------------------------------------------------------------------
# Reading the open data CSV
# ---------------------------------------------------------------------------

def read_open_data(path, years=None) -> dict:
    """{path, header, rows, years, newest} of the open data CSV. rows: one
    dict per authority row of a year from FIRST_YEAR (or of `years`), in
    file order: line, local_authority, code (local_authority_code),
    lad24cd_pub (the CSV's own LAD24CD), year (yyyy-yy), period (the
    reporting year as a string), status and the raw cc1a, cc2a, cc5a text.
    ValueError on a header lacking CSV_REQUIRED (named), a Year that is not
    yyyy-yy, a code that is not E06-E09, or a code twice in a year."""
    import pandas as pd
    p = Path(path)
    with open(p, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    missing = [c for c in CSV_REQUIRED if c not in header]
    if missing:
        raise ValueError(f"{p.name}: the header lacks {missing}; the reading "
                         "must be corrected deliberately")
    df = pd.read_csv(p, dtype=str, keep_default_na=False,
                     usecols=list(CSV_REQUIRED), encoding="utf-8-sig")
    bad = sorted({y for y in df["Year"] if not LABEL_RE.fullmatch(y)})
    if bad:
        raise ValueError(f"{p.name}: Year values {bad[:5]} are not yyyy-yy")
    every = sorted({y for y in df["Year"] if y >= FIRST_YEAR})
    for y in every:
        label_year(y)
    keep = set(years) if years is not None else set(every)
    rows, seen = [], set()
    for i, r in enumerate(df.itertuples(index=False)):
        if r.Year < FIRST_YEAR or r.Year not in keep:
            continue
        line = i + 2
        code = r.local_authority_code.strip()
        if not DISTRICT_RE.fullmatch(code):
            raise ValueError(f"{p.name} line {line}: {code!r} "
                             f"({r.local_authority}) is not an E06-E09 "
                             "authority code")
        if (code, r.Year) in seen:
            raise ValueError(f"{p.name} line {line}: {code} {r.Year} appears "
                             "twice")
        seen.add((code, r.Year))
        rows.append({"line": line, "local_authority": r.local_authority,
                     "code": code, "lad24cd_pub": r.LAD24CD.strip(),
                     "year": r.Year, "period": str(label_year(r.Year)),
                     "status": r.status.strip(), "cc1a": r.cc1a,
                     "cc2a": r.cc2a, "cc5a": r.cc5a})
    if not every:
        raise ValueError(f"{p.name}: no year from {FIRST_YEAR}")
    return {"path": p, "header": header, "rows": rows,
            "years": sorted({r["year"] for r in rows}), "newest": every[-1]}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def parse_rows(rows) -> list:
    """Each raw CSV row with its cells read: {code, lad24cd_pub, period,
    year, line, status, households_on_register, jointly_managed_register,
    reasonable_preference, flags {column: reason}}. ValueError (naming the
    line and the cell) on a cell that is not a count, Yes/No or a marker."""
    out = []
    for r in rows:
        w = f"line {r['line']} {r['code']} {r['year']}"
        h, fh = cell_int(r["cc1a"], f"{w} cc1a")
        j, fj = cell_yes_no(r["cc2a"], f"{w} cc2a")
        rp, frp = cell_int(r["cc5a"], f"{w} cc5a")
        flags = {c: f for c, f in ((HOUSEHOLDS, fh), (JOINTLY, fj),
                                   (REASONABLE, frp)) if f}
        out.append({"code": r["code"], "lad24cd_pub": r["lad24cd_pub"],
                    "period": r["period"], "year": r["year"],
                    "line": r["line"], "status": r["status"],
                    HOUSEHOLDS: h, JOINTLY: j, REASONABLE: rp,
                    "flags": flags, "rule_note": None})
    return out


def _rule_fires(rule, r) -> bool:
    y = int(r["period"])
    last = rule["last_year"]
    v = r[rule["column"]]
    return (r["code"] == rule["code"] and y >= rule["first_year"]
            and (last is None or y <= last)
            and isinstance(v, int) and not isinstance(v, bool) and v == 0)


def zero_rules(parsed) -> tuple:
    """(copies of the parsed rows with ZERO_RULES applied, [applied]). A
    rule fires on its publisher code, its years and a published 0 in its
    column, and nothing else: the value becomes NULL with the rule's flag
    and the row carries the rule's note. applied: [{rule, code, period,
    column}] sorted by period."""
    out, applied = [], []
    for r in parsed:
        r = dict(r, flags=dict(r["flags"]))
        for rule in ZERO_RULES:
            if _rule_fires(rule, r):
                r[rule["column"]] = None
                r["flags"][rule["column"]] = rule["flag"]
                r["rule_note"] = rule["note"]
                applied.append({"rule": rule["name"], "code": r["code"],
                                "period": r["period"],
                                "column": rule["column"]})
        out.append(r)
    applied.sort(key=lambda a: (a["period"], a["rule"]))
    return out, applied


def _getter(resolve):
    return resolve if callable(resolve) else (lambda c: resolve.get(c))


def value_flag(rec, flags) -> "str | None":
    """'column=reason' for each NULL value, '; '-joined; None if none."""
    parts = [f"{c}={flags[c]}" for c in VALUES
             if rec.get(c) is None and c in flags]
    return "; ".join(parts) or None


def successor_records(rows, resolve, period) -> list:
    """The reporting year's records, one per lad24cd (sorted), from its
    parsed rows (parse_rows, zero_rules). resolve: {code: lad24cd} or a
    callable. One source row: its values and flags. Several (a
    reorganisation): households_on_register and reasonable_preference the
    sum when every part has a number, else NULL 'part_missing';
    jointly_managed_register taken when every part agrees (value and
    reason), else NULL 'parts_disagree'. predecessor_codes: the sorted
    source codes; return_status their statuses in that order; imputed_cc1a
    None (set_imputed sets it for the ODS's year). ValueError on a row of
    another year, a code resolving to nothing, or a code twice."""
    get = _getter(resolve)
    groups = {}
    for r in rows:
        if r["period"] != str(period):
            raise ValueError(f"{period}: a row of {r['period']} (line "
                             f"{r['line']})")
        lad = get(r["code"])
        if not lad:
            raise ValueError(f"line {r['line']}: {r['code']} resolves to no "
                             "lad24cd")
        groups.setdefault(lad, []).append(r)
    out = []
    for lad, parts in groups.items():
        codes = [p["code"] for p in parts]
        if len(set(codes)) != len(codes):
            raise ValueError(f"{period}: {lad}: a source code appears twice "
                             f"({codes}); refusing to choose a winner")
        parts = sorted(parts, key=lambda p: p["code"])
        rec = {KEY: lad, PERIOD: str(period)}
        if len(parts) == 1:
            p = parts[0]
            flags = dict(p["flags"])
            for c in VALUES:
                rec[c] = p[c]
        else:
            flags = {}
            for c in (HOUSEHOLDS, REASONABLE):
                vals = [p[c] for p in parts]
                if all(v is not None for v in vals):
                    rec[c] = sum(vals)
                else:
                    rec[c] = None
                    flags[c] = "part_missing"
            jv = {(p[JOINTLY], p["flags"].get(JOINTLY)) for p in parts}
            if len(jv) == 1:
                v, f = jv.pop()
                rec[JOINTLY] = v
                if f:
                    flags[JOINTLY] = f
            else:
                rec[JOINTLY] = None
                flags[JOINTLY] = "parts_disagree"
        rec["value_flag"] = value_flag(rec, flags)
        rec["return_status"] = ";".join(p["status"] for p in parts)
        rec["imputed_cc1a"] = None
        rec["predecessor_codes"] = ";".join(p["code"] for p in parts)
        notes = [p["rule_note"] for p in parts if p.get("rule_note")]
        rec["rule_note"] = "; ".join(dict.fromkeys(notes)) or None
        out.append(rec)
    out.sort(key=lambda x: x[KEY])
    return out


def set_imputed(records, flags) -> None:
    """imputed_cc1a of each record: true if the publisher imputed cc1a for
    any of its source codes (imputed_flags), else false."""
    for r in records:
        codes = r["predecessor_codes"].split(";")
        r["imputed_cc1a"] = any((c, "cc1a") in flags for c in codes)


def rows_content_sha(records) -> str:
    """sha256 of a year's content: one line per record sorted by lad24cd,
    lad24cd|households_on_register|jointly_managed_register|
    reasonable_preference|value_flag|return_status|imputed_cc1a|
    predecessor_codes (NULL as '', booleans true/false), LF-joined, UTF-8.
    Nothing else is in it (live_source, labels and file names are not), so
    where a file was read never makes a new edition."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)
    lines = sorted("|".join([r[KEY]] + [cell(r.get(c)) for c in CONTENT])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The CSV against the ODS
# ---------------------------------------------------------------------------

def cross_check(rows_csv, ods) -> None:
    """ValueError unless the CSV's newest year equals the year ODS: the
    ODS's year is the CSV's newest, the same authorities, and for each cc1a
    and cc5a equal (as counts or markers) and cc2a equal (or, when the ODS
    has no cc2a column, every CSV cc2a of that year [z]). rows_csv: a
    read_open_data result (or its rows plus 'newest')."""
    newest = rows_csv["newest"]
    if ods["year"] != newest:
        raise ValueError(f"the ODS is for {ods['year']}, the CSV's newest year "
                         f"is {newest}")
    rows = {r["code"]: r for r in rows_csv["rows"] if r["year"] == newest}
    las = ods["las"]
    only_csv = sorted(set(rows) - set(las))
    only_ods = sorted(set(las) - set(rows))
    out = []
    if only_csv:
        out.append(f"{len(only_csv)} code(s) only in the CSV: {only_csv[:6]}")
    if only_ods:
        out.append(f"{len(only_ods)} code(s) only in the ODS: {only_ods[:6]}")
    for code in sorted(set(rows) & set(las)):
        r, o = rows[code], las[code]
        for var in ("cc1a", "cc5a"):
            a = cell_int(r[var], f"CSV {code} {newest} {var}")
            b = cell_int(o[var], f"ODS {code} {var}")
            if a != b:
                out.append(f"{code} {var}: CSV {r[var]!r}, ODS {o[var]!r}")
        if ods["has_cc2a"]:
            a = cell_yes_no(r["cc2a"], f"CSV {code} {newest} cc2a")
            b = cell_yes_no(o["cc2a"], f"ODS {code} cc2a")
            if a != b:
                out.append(f"{code} cc2a: CSV {r['cc2a']!r}, ODS "
                           f"{o['cc2a']!r}")
        elif _norm(r["cc2a"]) != "[z]":
            out.append(f"{code} cc2a: CSV {r['cc2a']!r}, but the ODS has no "
                       "cc2a column (expected [z] for every authority)")
    if out:
        raise ValueError(f"the CSV's {newest} differs from the year ODS in "
                         f"{len(out)} place(s): " + "; ".join(out[:10]))


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def resolve_codes(cur, codes, period, lad24cd_pub=None) -> tuple:
    """({publisher code: lad24cd}, problems) for one reporting year. In
    this order: geography.resolve(cur, '13', ...) (Barnsley/Sheffield;
    declared 'old', so E08000038/39 as a publisher code is a problem); then
    la_code_lookup new_unitary or merger rows with exactly one target in
    la_boundaries; then LOOKUP_GAPS (only while la_code_lookup has no row
    for the code). The resolved code must be in la_boundaries, else
    'UNEXPLAINED'. lad24cd_pub {code: the CSV's LAD24CD}: each must equal
    the resolved code, or be the new Barnsley/Sheffield code of it."""
    codes = set(codes)
    recode, problems = geography.resolve(cur, RUN_SOURCE, codes,
                                         by_period={period: codes})
    problems = list(problems)
    cur.execute("SELECT old_code, new_code FROM public.la_code_lookup "
                "WHERE change_type IN ('new_unitary', 'merger')")
    succ = {}
    for old, new in cur.fetchall():
        succ.setdefault(old, set()).add(new)
    cur.execute("SELECT DISTINCT old_code FROM public.la_code_lookup")
    known = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    out = {}
    for c in sorted(codes):
        if c in LOOKUP_GAPS and c in known:
            problems.append(f"LOOKUP_GAPS {c}: la_code_lookup now has a row "
                            "for it; remove the entry from LOOKUP_GAPS")
            continue
        r = geography.canonical(c, recode)
        if r not in valid:
            targets = {t for t in succ.get(r, ()) if t in valid}
            if len(targets) == 1 and len(succ[r]) == 1:
                r = targets.pop()
            elif c in LOOKUP_GAPS:
                r = LOOKUP_GAPS[c][0]
        if r not in valid:
            problems.append(f"UNEXPLAINED {c}: not in la_boundaries, not a "
                            "la_code_lookup recode, new_unitary or merger "
                            "with one target there, and not in LOOKUP_GAPS")
            continue
        out[c] = r
        pub = (lad24cd_pub or {}).get(c)
        renamed = any(pub == new and r == old
                      for old, new in geography.AREAS.values())
        if pub is not None and pub != r and not renamed:
            problems.append(f"{period}: {c} resolves to {r}, but the CSV's "
                            f"own LAD24CD says {pub}")
    return out, problems


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_RANK_RE = re.compile(r"\blatest update ([0-9]{4}-[0-9]{2}-[0-9]{2})\b")
_LEDGER_RE = re.compile(r"(.*) \(LAHS ([0-9]{4}-[0-9]{2}); latest update "
                        r"([0-9]{4}-[0-9]{2}-[0-9]{2}); periods ([0-9]{4}"
                        r"(?:, [0-9]{4})*)(?:; ods [^()]*)?\)")


def release_rank(file_or_source) -> "date | None":
    """The rank of a pair of files: a read ODS's own 'Latest update' date,
    or the 'latest update <yyyy-mm-dd>' an edition's or ledger row's
    source_file carries; None if it names none (a file name, URL or media
    id is never read for a rank)."""
    x = file_or_source
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    return date.fromisoformat(mm.group(1)) if mm else None


def ledger_source(where, rank, label, periods, ods=None) -> str:
    """A CSV's ledger source_file: where it was read (the final URL, or the
    path of a --file), the LAHS year its identity comes from, the rank, the
    periods it states and (optionally) the ODS read with it."""
    ps = ", ".join(str(p) for p in sorted(int(p) for p in periods))
    return (f"{where} (LAHS {label}; latest update {rank.isoformat()}; "
            f"periods {ps}" + (f"; ods {ods}" if ods else "") + ")")


def parse_ledger_source(s) -> "tuple | None":
    """(where, rank, [periods as strings]) of a ledger source_file, or
    None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return (mm.group(1), date.fromisoformat(mm.group(3)),
            mm.group(4).split(", "))


def content_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held period: the newest rank that stated it}: the tip edition's, or
    a later file the ledger records for the period."""
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, allow_older=False,
                    stranded=()) -> "list | None":
    """The periods the ledger records this CSV's (where, sha256) pair for,
    if that covers every period the file needs (its ledger source declares
    them); else None (the file is read and each period planned). A held
    period whose tip comes from a newer file is not needed unless
    allow_older; a stranded period always makes the file be read."""
    have, declared = set(), None
    for p, pairs in checked.items():
        for s, h in pairs:
            parsed = parse_ledger_source(s)
            if h == sha and parsed and parsed[0] == where:
                have.add(str(p))
                declared = parsed
    if declared is None:
        return None
    _, rank, need = declared
    need = set(need)
    if not allow_older:
        need = {p for p in need if not (ranks.get(p) is not None
                                        and ranks[p] > rank)}
    if not need or need & {str(x) for x in stranded}:
        return None
    return sorted(have) if need <= have else None


def plan_periods(periods, rank, ranks, checked, source, sha, *, recheck,
                 allow_older) -> tuple:
    """(new, revised, skipped), as S10: new periods not held (a new period
    before the newest held year is a back-fill, skipped unless
    allow_older); held periods whose tip comes from a newer file skipped as
    older unless allow_older (the older-file guard, on every path); held
    periods whose (source, sha) the ledger records skipped as checked
    unless --recheck names it. --recheck must be a held period of the file
    (ValueError)."""
    periods = sorted(str(p) for p in periods)
    if recheck is not None and (recheck not in periods or recheck not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held year this file "
                         f"states ({', '.join(periods)})")
    newest_held = max(ranks) if ranks else None
    new, revised, skipped = [], [], {}
    for p in periods:
        if p not in ranks:
            if newest_held is not None and p < newest_held and not allow_older:
                skipped[p] = (f"older: {p} is before the newest held year "
                              f"{newest_held}; back-filling is not done by "
                              "load")
                continue
            new.append(p)
            continue
        t = ranks[p]
        if t is not None and t > rank and not allow_older:
            skipped[p] = (f"older: its tip comes from a file of "
                          f"{rank_text(t)}, this file is {rank_text(rank)}")
            continue
        if recheck != p and (source, sha) in checked.get(p, ()):
            skipped[p] = "checked: this file is already in the ledger"
            continue
        revised.append(p)
    return new, revised, skipped


# ---------------------------------------------------------------------------
# Stop conditions and the named correction
# ---------------------------------------------------------------------------

def _over(old, new, pct) -> bool:
    """True when new moves from old by more than pct percent (exact)."""
    if old == new:
        return False
    if old == 0:
        return True
    return abs(new - old) * 100 > pct * old


def _by_key(records) -> dict:
    return {r[KEY]: r for r in records or ()}


def _num(v) -> bool:
    return v is not None and not isinstance(v, bool)


def _england(a, b) -> tuple:
    """(total in a, total in b, authorities) of households_on_register over
    the authorities with a figure in both (a NULL is never counted as 0)."""
    common = [k for k in sorted(set(a) & set(b))
              if _num(a[k][HOUSEHOLDS]) and _num(b[k][HOUSEHOLDS])]
    return (sum(a[k][HOUSEHOLDS] for k in common),
            sum(b[k][HOUSEHOLDS] for k in common), len(common))


def zero_null_flips(new, tip) -> list:
    """[(lad24cd, column, tip value, new value)] of value cells going from
    0 to NULL or NULL to 0 against the held edition (rule 1.10)."""
    nk, tk = _by_key(new), _by_key(tip)
    out = []
    for k in sorted(set(nk) & set(tk)):
        for c in (HOUSEHOLDS, REASONABLE):
            a, b = tk[k].get(c), nk[k].get(c)
            if (a == 0 and _num(a) and b is None) or \
                    (a is None and b == 0 and _num(b)):
                out.append((k, c, a, b))
    return out


def period_problems(new, tip, prev, *, kind) -> list:
    """The stop conditions of one reporting year (empty = fine); see the
    module docstring. Problems starting with PARTIAL are released by
    nothing. A 0/NULL change is zero_null_flips' business (the caller)."""
    g = globals()
    out = []
    nk = _by_key(new)
    want = g["EXPECTED_AREAS"]
    if len(nk) < want:
        out.append(f"{PARTIAL}: {len(nk)} authorities, expected {want} (a "
                   "partial file never replaces a fuller edition)")
    if kind == "new":
        if not prev:
            return out
        pk = _by_key(prev)
        a, b, n = _england(pk, nk)
        if _over(a, b, g["NEW_TOTAL_PCT"]):
            out.append(f"England households_on_register {a:,} -> {b:,} over "
                       f"the {n} authorities with a figure in both years "
                       f"(limit {g['NEW_TOTAL_PCT']}%)")
        moving = [k for k in sorted(set(pk) & set(nk))
                  if _num(pk[k][HOUSEHOLDS]) and _num(nk[k][HOUSEHOLDS])
                  and _over(pk[k][HOUSEHOLDS], nk[k][HOUSEHOLDS],
                            g["NEW_AREA_PCT"])]
        if len(moving) > g["NEW_AREAS_MOVING"]:
            out.append(f"{len(moving)} authorities' households_on_register "
                       f"move by more than {g['NEW_AREA_PCT']}% against the "
                       f"previous held year (limit {g['NEW_AREAS_MOVING']}), "
                       f"e.g. {', '.join(moving[:6])}")
        return out
    tk = _by_key(tip)
    gone = sorted(set(tk) - set(nk))
    if gone:
        out.append(f"{PARTIAL}: {len(gone)} authorit(ies) of the held edition "
                   f"missing from the file (a partial file never replaces a "
                   f"fuller edition): {', '.join(gone[:8])}")
    changed = [k for k in sorted(set(tk) | set(nk))
               if k not in tk or k not in nk
               or any(tk[k][c] != nk[k][c] or _num(tk[k][c]) != _num(nk[k][c])
                      for c in VALUES)]
    a, b, n = _england(tk, nk)
    if _over(a, b, g["REV_TOTAL_PCT"]):
        out.append(f"England households_on_register {a:,} -> {b:,} over the "
                   f"{n} authorities with a figure in both editions (limit "
                   f"{g['REV_TOTAL_PCT']}%)")
    if len(changed) > g["REV_MAX_AREAS"]:
        out.append(f"{len(changed)} authorities change against the held "
                   f"edition (limit {g['REV_MAX_AREAS']}), e.g. "
                   f"{', '.join(changed[:6])}")
    flips = {(k, c) for k, c, _, _ in zero_null_flips(new, tip)}
    nulls = [f"{k} {c}" for k in sorted(set(tk) & set(nk)) for c in VALUES
             if (tk[k][c] is None) != (nk[k][c] is None)
             and (k, c) not in flips]
    if nulls:
        out.append(f"{len(nulls)} value(s) go to or from NULL against the "
                   f"held edition, e.g. {', '.join(nulls[:6])}")
    return out


def classify_changes(new, tip, period) -> dict:
    """{group: [lad24cd]} of the value changes of a revised year against the
    held edition: cc5a_filled (reasonable_preference NULL -> anything else),
    allerdale (households or cc2a of ALLERDALE_ZERO's successor in its
    years), predecessor_sum (households or cc2a of a record built from
    several predecessors), other (everything else, a key on one side
    included). A key may be in two groups (cc5a_filled and one other)."""
    nk, tk = _by_key(new), _by_key(tip)
    g = {"cc5a_filled": [], "predecessor_sum": [], "allerdale": [],
         "other": []}
    rule = ALLERDALE_ZERO
    y = int(period)
    for k in sorted(set(nk) | set(tk)):
        if k not in nk or k not in tk:
            g["other"].append(k)
            continue
        t, n = tk[k], nk[k]

        def differs(c):
            return t[c] != n[c] or _num(t[c]) != _num(n[c]) or \
                (t[c] is None) != (n[c] is None)
        if differs(REASONABLE):
            g["cc5a_filled" if t[REASONABLE] is None else "other"].append(k)
        if differs(HOUSEHOLDS) or differs(JOINTLY):
            if k == rule["successor"] and \
                    rule["first_year"] <= y <= rule["last_year"]:
                g["allerdale"].append(k)
            elif ";" in (n.get("predecessor_codes") or ""):
                g["predecessor_sum"].append(k)
            else:
                g["other"].append(k)
    return g


def correction_problems(name, period, groups) -> list:
    """Problems (empty = covered) of releasing a revised year's changes
    (classify_changes) under ACKNOWLEDGED_CORRECTIONS[name]: the year must
    be listed, nothing may be in 'other', and each group's count must equal
    the named count exactly (an unlisted group expects 0)."""
    a = globals()["ACKNOWLEDGED_CORRECTIONS"][name]
    if period not in a["periods"]:
        return [f"{name} names no changes for {period}"]
    want = a["periods"][period]
    out = []
    if groups["other"]:
        out.append(f"{len(groups['other'])} authorit(ies) change outside the "
                   f"named groups: {', '.join(groups['other'][:6])}")
    for grp in ("cc5a_filled", "predecessor_sum", "allerdale"):
        # an expected count: a group the correction does not list expects none
        w = want.get(grp, 0)  # not a source value
        got = len(groups[grp])
        if got != w:
            out.append(f"{grp}: {got} authorit(ies), {name} expects exactly "
                       f"{w}")
    return out


def _differs(new, old) -> bool:
    nk, ok = _by_key(new), _by_key(old)
    if set(nk) != set(ok):
        return True
    return any(nk[k].get(c) != ok[k].get(c)
               or _num(nk[k].get(c)) != _num(ok[k].get(c))
               for k in nk for c in CONTENT)


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

SPEC = core.EditionSpec(
    name="s13",
    live_table="la_housing_register",
    editions_table="la_housing_register_editions",
    key_cols=(KEY,),
    period_col=PERIOD,
    value_cols=VALUE_TYPES,
    extra_cols=EXTRA_TYPES + PROVENANCE_TYPES,
    refresh_cols=VALUES + ("source", "loaded_at"),
    refresh_from=(("source", "live_source"), ("loaded_at", "loaded_at")),
    key_types=((KEY, "varchar(9) NOT NULL"), (PERIOD, "integer NOT NULL")),
    fk_la_boundaries=True,
    refresh_key_changes=False,     # the key set is the 296 every year
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S13 release label comes from the files; store "
                     "through apply_year(...)")


def check_records(records, period) -> None:
    """ValueError unless the year's records are whole: each of this year,
    one per lad24cd, every content column present (a NULL value carries its
    reason)."""
    seen = set()
    for r in records:
        if r.get(PERIOD) != period:
            raise ValueError(f"{period}: a record of year {r.get(PERIOD)!r}")
        if r[KEY] in seen:
            raise ValueError(f"{period}: {r[KEY]} appears twice")
        missing = [c for c in CONTENT if c not in r]
        if missing:
            raise ValueError(f"{period}: {r[KEY]} lacks {missing}")
        flags = dict(x.split("=", 1) for x in (r["value_flag"] or "").split(
            "; ") if x)
        bare = [c for c in VALUES if r[c] is None and c not in flags]
        if bare:
            raise ValueError(f"{period}: {r[KEY]} has NULL {bare} without a "
                             "reason in value_flag")
        seen.add(r[KEY])


PROFILE = pe.Profile(
    spec=SPEC, value_cols=CONTENT, run_agent=RUN_AGENT, run_source=RUN_SOURCE,
    heading="S13 MHCLG LAHS housing register (la_housing_register)",
    default_source_file="MHCLG LAHS open data",
    expected_areas=EXPECTED_AREAS, release_label=_no_label,
    content_sha256=rows_content_sha, check_records=check_records,
    file_checks=True, savepoint="s13_period",
    example_label="(households, cc2a, cc5a, flags...)")


def profile() -> pe.Profile:
    """PROFILE bound to the current SPEC and EXPECTED_AREAS (looked up when
    called, so tests can swap them)."""
    g = globals()
    return dataclasses.replace(PROFILE, spec=g["SPEC"],
                               expected_areas=g["EXPECTED_AREAS"])


def _late(name: str):
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: the editions table, its append-only triggers and the file-check
    ledger. Idempotent; no value is written; the live table is not
    touched."""
    prof = profile()
    core.create_schema(cur, prof.spec)
    pe.create_file_checks(cur, prof)


def insert_live(cur, profile, period: str, records: list) -> None:
    """A new year's live rows: the key, the period, the three values and
    source (from each record's live_source); the editions-only columns are
    not written; loaded_at takes its default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = (KEY, spec.period_col) + VALUES + ("source",)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [(r[KEY], period) + tuple(r[c] for c in VALUES) + (r["live_source"],)
         for r in records], page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a year's edition records: source_file, the release label and
    the files read ((ledger source, sha256),)."""
    source_file: str
    label: str
    files: tuple


def _live_source(label, rec) -> str:
    return label + (f" [{rec['rule_note']}]" if rec.get("rule_note") else "")


def _tip_live_source(cur, spec, period) -> dict:
    tip = core.chain_tip(cur, spec, period)
    cur.execute(f"SELECT {KEY}, live_source FROM public.{spec.editions_table} "
                f"WHERE {spec.period_col} = %s AND edition = %s",
                (period, tip))
    return dict(cur.fetchall())


def apply_year(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for one year, then its ledger row, inside the
    caller's per-period savepoint (s13_period): a new year's edition 1, live
    rows and ledger row commit or roll back together. Each record's
    live_source is the edition's label plus its rule note; a stranded year
    (editions, no live rows) gets its live rows with the tip edition's."""
    kind = pe.classify_period(cur, profile, period, records)
    if kind == LIVE_MISSING:
        src = _tip_live_source(cur, profile.spec, period)
        records = [dict(r, live_source=src.get(r[KEY])) for r in records]
    else:
        records = [dict(r, live_source=_live_source(info.label, r))
                   for r in records]
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
                           source_file=info.source_file,
                           classify=lambda *a, **kw: kind,
                           insert=_late("insert_live"))
    tip = core.chain_tip(cur, prof.spec, period)
    for src_file, sha in info.files:
        pe.record_file_check(cur, prof, period, src_file, sha, kind, tip)
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
        r = session.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=300, **kw)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001 (any failure halts)
        halt(f"GET {url} failed, nothing downloaded: {e}")
    return r


def fetch_json(path, session=None) -> dict:
    """The GOV.UK content API reply for a page path (a read-only GET)."""
    return json.loads(_get(_session(session), API + path).text)


def _abs_url(url) -> str:
    return url if urlparse(url).scheme else GOV_UK + url


def _opens(path, kind) -> None:
    """Raise unless the file is what it claims: the open data CSV (its
    header names local_authority_code and cc1a) or an OpenDocument
    spreadsheet with a Local_Authority_Data sheet."""
    if kind == "csv":
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            head = f.readline()
        if "local_authority_code" not in head or "cc1a" not in head:
            raise ValueError("its first line is not the open data header")
        return
    with zipfile.ZipFile(path) as z:
        if z.read("mimetype").strip() != ODS_MIME:
            raise ValueError("not an OpenDocument spreadsheet")
        if b'table:name="Local_Authority_Data"' not in z.read("content.xml"):
            raise ValueError("no Local_Authority_Data sheet")


def fetch(url, dest, session=None, kind="csv") -> tuple:
    """Download url into the directory dest (redirects followed) and return
    (path to read, final URL). kind 'csv' or 'ods'. The file name is the
    final URL's. Checked before it is kept: more than MIN_FILE_BYTES, the
    kind's suffix, and _opens. A same-named file with the same content is
    kept as it is; one with different content is never replaced: the
    download is saved beside it as <stem>-<sha8><suffix> and that is the
    one read. An existing name is never a reason to skip the download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    name = Path(urlparse(final).path).name
    suffix = Path(name).suffix.lower()
    if suffix != f".{kind}":
        halt(f"{final}: not a .{kind} file; nothing written")
    if len(body) <= MIN_FILE_BYTES:
        halt(f"{final}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written; saw {body[:100]!r}")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    tmp = dest / (Path(name).stem + ".download.tmp" + suffix)
    tmp.write_bytes(body)
    try:
        _opens(tmp, kind)
    except Exception as e:  # noqa: BLE001
        tmp.unlink()
        halt(f"{final}: does not open as the LAHS {kind} ({e}); nothing kept")
    path = target
    if target.exists():
        old_sha = content_sha256(target)
        if old_sha == new_sha:
            tmp.unlink()
            print(f"{name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
        else:
            path = dest / f"{target.stem}-{new_sha[:8]}{target.suffix}"
            if path.exists() and content_sha256(path) == new_sha:
                tmp.unlink()
                print(f"NOTE: {name} differs from this download; "
                      f"{path.name} already holds it (sha256 "
                      f"{new_sha[:16]})")
            else:
                tmp.replace(path)
                print(f"NOTE: {name} already exists with different content "
                      f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
                      f"untouched and saved the new download as {path.name}")
    else:
        tmp.replace(target)
        print(f"downloaded {name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    return path, final


def _file_source(path) -> str:
    """Where a --file was read: the path under the repo (data/...), else the
    absolute path."""
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
    """The year's rows as records: the given edition's (every content
    column and live_source), or the live table's (edition None: the three
    values, source as live_source, the editions-only columns None)."""
    if edition is None:
        names = (KEY,) + VALUES + ("source",)
        cur.execute(f"SELECT {', '.join(names)} FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (period,))
        out = []
        for row in cur.fetchall():
            r = dict(zip(names, row))
            r["live_source"] = r.pop("source")
            for c in EXTRAS:
                r[c] = None
            out.append(r)
    else:
        names = (KEY,) + CONTENT + ("live_source",)
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (period, edition))
        out = [dict(zip(names, row)) for row in cur.fetchall()]
    for r in out:
        r[spec.period_col] = str(period)
    return out


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
                         "rank": release_rank(sf)}
    return out


def legacy_tips(cur, spec) -> dict:
    """Before the editions table exists: {held live period: {rank}}, the
    February 2026 file's rank (LEGACY_RANK)."""
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    return {pe._p(p): {"edition": None, "source_file": None,
                       "rank": LEGACY_RANK} for (p,) in cur.fetchall()}


def ledger_checks(cur, prof) -> dict:
    pc = prof.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(prof)}")
    out = {}
    for p, f, s in cur.fetchall():
        out.setdefault(pe._p(p), set()).add((f, s))
    return out


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '13'). Called only on committed load, migrate-legacy and
    restore-edition runs, partial runs included."""
    pe.log_run(cur, profile(), rows_written, notes, started_at)


def cmd_ddl(args) -> int:
    return pe.run_ddl(profile(), args, connect=_late("_conn"),
                      table_exists=_late("table_exists"),
                      create_schema=lambda cur: create_all(cur))


def status_lines(cur, prof) -> list:
    s = prof.spec
    lt = pe.file_checks_table(prof)
    lines = []
    for p, t in sorted(tip_info(cur, s).items()):
        line = f"  {p}: tip edition {t['edition']} ({rank_text(t['rank'])})"
        cur.execute(f"SELECT source_file, outcome FROM public.{lt} WHERE "
                    f"{s.period_col} = %s ORDER BY id DESC LIMIT 1", (p,))
        last = cur.fetchone()
        line += (f"; ledger latest: {last[1]} {last[0]}" if last
                 else "; no ledger row")
        lines.append(line)
    return lines


def cmd_status(args) -> int:
    """status: what needs action; exit 1 if anything, or (with a clean
    message, nothing created) if the editions table or ledger does not
    exist."""
    prof = profile()
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            missing = [t for t in (prof.spec.editions_table,
                                   pe.file_checks_table(prof))
                       if not table_exists(cur, t)]
            if missing:
                print(f"status: {' and '.join(missing)} "
                      f"{'does' if len(missing) == 1 else 'do'} not exist "
                      "yet; run `ddl --commit`, then `migrate-legacy FILE "
                      "--commit` (S13 has no sync-new)")
                return 1
            st = pe.status(cur, prof)
            lines = status_lines(cur, prof)
    finally:
        conn.rollback()
        conn.close()
    print(pe.format_status(prof, st, lines))
    return 0 if st["ok"] else 1


def _tables_exist(cur) -> bool:
    prof = profile()
    for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
        if not table_exists(cur, t):
            halt(f"{t} does not exist yet; run `ddl --commit`, then "
                 "`migrate-legacy FILE --commit`")
    return True


def live_equals_tip(cur, spec, periods) -> list:
    """Problems (empty = fine): for each period, the live rows equal the
    tip edition on the key, the year and the three values, NULL-safe, both
    ways. (load_checks.check_latest_equals_live compares every data column
    by name, and the editions-only columns are not live columns.)"""
    cols = ", ".join((KEY, spec.period_col) + VALUES)
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
            bad.append(f"{pe._p(p)} ed{ed}: {counts[0]} edition-only, "
                       f"{counts[1]} live-only rows")
    return bad


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift YYYY] [--commit | --simulate]: the
    engine's refresh (period_editions.run_refresh_latest's steps: each
    year's latest edition copied into live on the rows whose values differ:
    the three values, source from live_source and loaded_at, under the
    before/after hash guard), with S13's after-check live_equals_tip."""
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            _tables_exist(cur)
            accept = pe._accept_periods(cur, spec, tuple(args.accept_drift
                                                         or ()))
            counts = core.refresh_counts(cur, spec, accept)
            print("rows refresh-latest would write: "
                  + (", ".join(f"{pe._p(p)}={n}" for p, n in counts.items())
                     or "none")
                  + f" (total {sum(counts.values())})")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            pe._refresh_guard(cur, spec, accept)
            res = core.refresh_latest(cur, spec, accept)
            if res["updated"]:
                bad = live_equals_tip(cur, spec, sorted(res["updated"]))
                if bad:
                    halt("refresh-latest: live differs from the latest edition "
                         "after the refresh, rolled back: " + "; ".join(bad[:6]))
        pe.finish(conn, args, f"{res['rows']} live rows refreshed in "
                              f"{sorted(pe._p(x) for x in res['updated'])}; "
                              "before/after guard passed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def _year_arg(text) -> str:
    if not re.fullmatch(r"[0-9]{4}", str(text or "")):
        raise argparse.ArgumentTypeError(f"not a year YYYY: {text!r}")
    return str(text)


def _label_arg(text) -> str:
    try:
        label_year(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return str(text)


def _read_csv(path) -> dict:
    try:
        return read_open_data(path)
    except ValueError as e:
        halt(f"{e}; nothing stored")


def _read_ods(path) -> dict:
    try:
        return read_year_ods(path)
    except ValueError as e:
        halt(f"{e}; nothing stored")


def _collection():
    coll = fetch_json(COLLECTION_PATH)
    try:
        check_collection(coll)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    return coll


def _print_page(page, path):
    print(f"page: {page.get('title')} ({GOV_UK}{path}); updated "
          f"{str(page.get('public_updated_at'))[:10]}")
    for ts, note in change_history(page)[:4]:
        print(f"  change_history {ts}: {note[:200]}")


def _year_page(base, label):
    page = fetch_json(base)
    if page_label(page) != label:
        halt(f"{GOV_UK}{base}: the page title {page.get('title')!r} is not "
             f"the {label} year page")
    _print_page(page, base)
    return page


def _download_ods(page, label):
    try:
        att = year_ods_attachment(page)
    except ValueError as e:
        halt(f"the {label} year page: {e}")
    if att["year"] != label:
        halt(f"the attachment {att['title']!r} is for {att['year']}, the page "
             f"is {label}")
    print(f"  attachment: {att['title']!r} {att['url']}")
    return fetch(_abs_url(att["url"]), RAW_DIR, kind="ods")


def _source(args) -> dict:
    """The files the run reads: {path, where, how, page_label, file_name,
    od, ods_path, ods_where, discovered}. --file reads a local CSV (and
    --ods a local ODS; otherwise the CSV's newest year page gives the ODS,
    downloaded); --no-page needs --ods. Without --file the content API finds
    the open data page's CSV and its newest year's ODS, both downloaded to
    RAW_DIR (also in a preview)."""
    out = {"path": None, "where": None, "how": None, "page_label": None,
           "file_name": None, "od": None, "ods_path": None,
           "ods_where": None, "discovered": None}
    if args.ods and not args.file:
        halt("--ods is only for --file (the year ODS read with a local CSV)")
    if args.no_page and not args.file:
        halt("--no-page is only for --file (a local CSV whose year page is "
             "not read)")
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"file given: {path}; nothing downloaded; its identity is the "
              "year ODS")
        od = _read_csv(path)
        label = od["newest"]
        if args.release is not None and args.release != label:
            halt(f"file identity check failed, nothing stored: --release "
                 f"{args.release}, the CSV's newest year is {label}")
        out.update(path=path, where=_file_source(path), how="local file",
                   file_name=path.name, od=od)
        if args.ods:
            op = Path(args.ods)
            if not op.is_file():
                halt(f"--ods {args.ods}: no such file")
            out.update(ods_path=op, ods_where=_file_source(op))
        if args.no_page:
            if not args.ods:
                halt("--file --no-page needs --ods PATH: the year ODS is the "
                     "CSV's identity (its Cover's year and Latest update)")
            out.update(how="local file, --no-page: the year page was not "
                       "read")
            print("NOTE: --no-page: the year page is not read")
            return out
        coll = _collection()
        try:
            lb, base = year_page_for(coll, label)
        except ValueError as e:
            halt(f"{GOV_UK}{COLLECTION_PATH}: {e}; the year page of the CSV's "
                 f"newest year ({label}) was not found. Give --no-page --ods "
                 "PATH to load the files without it (recorded as such)")
        page = _year_page(base, lb)
        out["page_label"] = lb
        if not args.ods:
            print(f"downloading the {lb} ODS to {RAW_DIR} (also in a preview; "
                  "the database is not written)")
            op, final = _download_ods(page, lb)
            print(f"  final URL: {final}")
            out.update(ods_path=op, ods_where=final)
        return out
    odp = fetch_json(OPEN_DATA_PATH)
    try:
        att = open_data_attachment(odp)
    except ValueError as e:
        halt(f"{GOV_UK}{OPEN_DATA_PATH}: {e}")
    _print_page(odp, OPEN_DATA_PATH)
    label = att["year"]
    print(f"  attachment: {att['title']!r} {att['url']}")
    for t in att["ignored"]:
        print(f"  not read: {t!r}")
    if args.release is not None and args.release != label:
        halt(f"--release {args.release}: the open data file on the page runs "
             f"to {label}; nothing stored")
    coll = _collection()
    try:
        newest_page, _ = latest_year_page(coll)
        lb, base = year_page_for(coll, label)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    if newest_page > label:
        print(f"NOTE: the collection lists the {newest_page} year page, but "
              f"the open data file still ends in {label}: the open data has "
              f"not caught up; {label} is read")
    page = _year_page(base, lb)
    print(f"downloading to {RAW_DIR} (also in a preview; the database is not "
          "written)")
    path, final = fetch(_abs_url(att["url"]), RAW_DIR, kind="csv")
    print(f"  final URL: {final}")
    op, ofinal = _download_ods(page, lb)
    print(f"  final URL: {ofinal}")
    out.update(path=path, where=final, page_label=lb, ods_path=op,
               ods_where=ofinal, discovered=label,
               how=("release " + label + " given" if args.release
                    else "newest open data (content API)"),
               file_name=Path(urlparse(final).path).name)
    return out


def _label(ods, src, sha, how, *, notes=()) -> str:
    return (f"MHCLG LAHS open data 1978-79 to {ods['year']}; LAHS "
            f"{ods['year']} latest update {ods['rank'].isoformat()}; "
            f"{src['where']} sha256 {sha[:16]}; {how}"
            + "".join(f"; {n}" for n in notes if n))


def edition_source(ods, src, ods_sha, note="") -> str:
    return (f"{src['file_name']}; LAHS {ods['year']}; latest update "
            f"{ods['rank'].isoformat()}; {src['where']}; ods "
            f"{Path(src['ods_path']).name} sha256 {ods_sha[:16]}"
            + (f"; {note}" if note else ""))


def _report(od, ods) -> None:
    n_imp = sum(1 for r in ods["imputations"] if _norm(r[1]) == "cc1a")
    print(f"{Path(od['path']).name}: {len(od['rows']):,} authority rows for "
          f"{od['years'][0]} to {od['years'][-1]}; "
          f"{Path(ods['path']).name}: {ods['title']!r}; latest update "
          f"{ods['latest_update']:%d %B %Y}; next update "
          f"{ods['next_update']!r}; markers "
          + ", ".join(f"[{k}] {v!r}" for k, v in sorted(ods["markers"].items()))
          + f"; {len(ods['las'])} authorities; cc2a column "
          + ("present" if ods["has_cc2a"] else "absent (the CSV's cc2a for "
             f"{ods['year']} must be [z])")
          + f"; {n_imp} imputed cc1a cell(s)")


def _identity(od, ods, page_lb, release) -> None:
    bad = []
    if ods["year"] != od["newest"]:
        bad.append(f"the ODS is for {ods['year']}, the CSV's newest year is "
                   f"{od['newest']}")
    if page_lb is not None and page_lb != od["newest"]:
        bad.append(f"the year page is {page_lb}, the CSV's newest year is "
                   f"{od['newest']}")
    if release is not None and release != od["newest"]:
        bad.append(f"--release {release}, the CSV's newest year is "
                   f"{od['newest']}")
    if bad:
        halt("file identity check failed, nothing stored: " + "; ".join(bad))
    try:
        cross_check(od, ods)
    except ValueError as e:
        halt(f"cross-check failed, nothing stored: {e}")
    print(f"cross-checked: the CSV's {od['newest']} equals the year ODS on "
          "cc1a, cc2a and cc5a for all "
          f"{len(ods['las'])} authorities")


def _build(cur, od, ods) -> tuple:
    """({period: records}, applied zero rules): cells parsed, the zero
    rules applied, codes resolved per year, successors built, imputed flags
    for the ODS's year. Halts on any cell or geography problem."""
    try:
        parsed, applied = zero_rules(parse_rows(od["rows"]))
        flags = imputed_flags(ods)
    except ValueError as e:
        halt(f"{e}; nothing stored")
    newest = str(label_year(ods["year"]))
    out, used, recoded, problems = {}, set(), set(), []
    for p in sorted({r["period"] for r in parsed}):
        rows = [r for r in parsed if r["period"] == p]
        rmap, probs = resolve_codes(cur, {r["code"] for r in rows}, p,
                                    {r["code"]: r["lad24cd_pub"]
                                     for r in rows})
        problems += probs
        if probs:
            continue
        used |= {c for c in rmap if c in LOOKUP_GAPS}
        recoded |= {(c, r) for c, r in rmap.items() if r != c}
        try:
            recs = successor_records(rows, rmap, p)
        except ValueError as e:
            halt(f"{e}; nothing stored")
        if p == newest:
            set_imputed(recs, flags)
        out[p] = recs
    if problems:
        halt("geography check failed, nothing stored: "
             + "; ".join(dict.fromkeys(problems)))
    multi = sorted({c for c, r in recoded if c not in LOOKUP_GAPS})
    print(f"codes resolved (geography.resolve, la_code_lookup): {len(multi)} "
          "publisher code(s) to a successor or canonical code")
    if used:
        print("LOOKUP_GAPS used (la_code_lookup lacks them; the CSV's own "
              "LAD24CD agrees): " + ", ".join(
                  f"{c} -> {LOOKUP_GAPS[c][0]}" for c in sorted(used)))
    for rule in ZERO_RULES:
        hits = [a for a in applied if a["rule"] == rule["name"]]
        print(f"zero rule {rule['name']} ({rule['code']} "
              f"{rule['first_year']}-{rule['last_year'] or 'onward'}, "
              f"{rule['column']} 0 -> NULL {rule['flag']}): "
              + (", ".join(f"{a['code']} {a['period']}" for a in hits)
                 or "did not fire") + f"; evidence: {rule['evidence']}")
    return out, applied


def _inherit_imputed(cur, spec, by_period, tips, newest) -> None:
    """For a held year other than the ODS's, imputed_cc1a is not checked by
    this run: a record keeps the tip edition's flag where its
    households_on_register and predecessor_codes are unchanged (the flag
    belongs to that figure), else it stays NULL (not checked)."""
    for p, recs in by_period.items():
        if p == newest or p not in tips:
            continue
        tk = _by_key(records(cur, spec, p, tips[p]["edition"]))
        for r in recs:
            t = tk.get(r[KEY])
            if r["imputed_cc1a"] is None and t is not None and \
                    t[HOUSEHOLDS] == r[HOUSEHOLDS] and \
                    _num(t[HOUSEHOLDS]) == _num(r[HOUSEHOLDS]) and \
                    t["predecessor_codes"] == r["predecessor_codes"]:
                r["imputed_cc1a"] = t["imputed_cc1a"]


def cmd_load(args) -> int:
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            lt = pe.file_checks_table(prof)
            ready = {spec.editions_table: table_exists(cur, spec.editions_table),
                     lt: table_exists(cur, lt)}
            has_ed = all(ready.values())
            missing = [t for t, ok in ready.items() if not ok]
            if writing and not has_ed:
                halt(f"missing {', '.join(missing)}; run `ddl --commit` and "
                     "`migrate-legacy FILE --commit` first")
            if has_ed:
                _, new_live, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new_live:
                    halt(f"{spec.live_table}: live years with no editions "
                         f"{[pe._p(x) for x in new_live]}; run migrate-legacy "
                         "first")
                tips = tip_info(cur, spec)
                checked = ledger_checks(cur, prof)
                stranded = pe.live_missing_periods(cur, prof)
            else:
                print(f"NOTE: {', '.join(missing)} not present: no editions "
                      "table yet; this preview compares the file with the "
                      "LIVE table")
                tips, checked, stranded = legacy_tips(cur, spec), {}, []
            ranks = tip_ranks(tips, checked)
            held = sorted(ranks)
            print("held years: " + (", ".join(held) or "none"))
            src = _source(args)
            for line in discovery_note(src["discovered"], held):
                print(line)
            path = src["path"]
            sha = content_sha256(path)
            print(f"{Path(path).name}: sha256 {sha[:16]}"
                  + ("; byte-identical to the held February 2026 file"
                     if sha == LEGACY_FILE["sha256"] else "")
                  + ("; byte-identical to the held June 2026 file"
                     if LEGACY_JUNE and sha == LEGACY_JUNE["sha256"] else ""))
            od = src["od"]
            if has_ed and args.recheck is None:
                done = ledger_complete(checked, src["where"], sha, ranks,
                                       allow_older=args.allow_older_file,
                                       stranded=stranded)
                if done:
                    print(f"the CSV's (source, sha256) is already in the "
                          f"ledger for {', '.join(done)}; "
                          + ("nothing compared" if od else "nothing parsed")
                          + " (--recheck YYYY reads it again)")
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            od = od or _read_csv(path)
            ods = _read_ods(src["ods_path"])
            ods_sha = content_sha256(src["ods_path"])
            print(f"{Path(src['ods_path']).name}: sha256 {ods_sha[:16]}")
            _identity(od, ods, src["page_label"], args.release)
            _report(od, ods)
            by_period, applied = _build(cur, od, ods)
            if has_ed:
                _inherit_imputed(cur, spec, by_period, tips,
                                 str(label_year(ods["year"])))
            periods = sorted(by_period)
            lsrc = ledger_source(src["where"], ods["rank"], ods["year"],
                                 periods, ods=f"{Path(src['ods_path']).name} "
                                 f"sha256 {ods_sha[:16]}")
            chk = {p: s for p, s in checked.items() if p not in stranded}
            try:
                new, revised, skipped = plan_periods(
                    periods, ods["rank"], ranks, chk, lsrc, sha,
                    recheck=args.recheck, allow_older=args.allow_older_file)
            except ValueError as e:
                halt(str(e))
            rc = _run_periods(args, conn, cur, prof, has_ed, src, ods,
                              ods_sha, by_period, new, revised, skipped, tips,
                              ranks, held, sha, lsrc, applied)
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


def _counts(groups) -> str:
    return ", ".join(f"{g} {len(v)}" for g, v in groups.items() if v) or \
        "no changes"


def _run_periods(args, conn, cur, prof, has_ed, src, ods, ods_sha, by_period,
                 new, revised, skipped, tips, ranks, held, sha, lsrc,
                 applied) -> int:
    started = datetime.now(timezone.utc)
    spec = prof.spec
    rank = ods["rank"]
    older_note = {}
    for p in revised + new:
        t = ranks.get(p)
        if t is not None and t > rank:
            older_note[p] = (f"--allow-older-file given; {p}'s tip comes from "
                             f"a file of {rank_text(t)}, this file is "
                             f"{rank_text(rank)}")
        elif p in new and held and p < max(held):
            older_note[p] = (f"--allow-older-file given; {p} is before the "
                             f"newest held year {max(held)}")
        if p in older_note:
            print(f"NOTE: {older_note[p]}")
    print(f"planned: new {new or 'none'}; compare {revised or 'none'}; "
          "skipped " + (", ".join(f"{p} ({r})" for p, r in sorted(
              skipped.items())) or "none"))
    periods = sorted(new + revised)
    ack = set(args.acknowledge or ())
    reissue = set(args.accept_reissue or ())
    stray = sorted((ack | reissue) - set(periods))
    if stray:
        halt(f"--acknowledge/--accept-reissue {', '.join(stray)}: not years "
             f"this run compares or stores ({', '.join(periods) or 'none'}); "
             "nothing stored")
    corr = args.acknowledge_correction
    if corr and not set(ACKNOWLEDGED_CORRECTIONS[corr]["periods"]) & \
            set(revised):
        halt(f"--acknowledge-correction {corr} names years "
             f"{', '.join(sorted(ACKNOWLEDGED_CORRECTIONS[corr]['periods']))}"
             f", none of which this run compares as a held year (compared: "
             f"{', '.join(revised) or 'none'}); nothing stored")
    if not periods:
        if skipped and all(r.startswith("older") for r in skipped.values()):
            halt(f"older file: every year the file states is skipped; the "
                 f"file is {rank_text(rank)}, older than what is held for "
                 f"{', '.join(sorted(skipped))}; storing it would record an "
                 "older file. If this is deliberate, re-run with "
                 "--allow-older-file")
        print("nothing to do")
        return 0
    problems, infos, acked, reissued, corrected = {}, {}, {}, {}, {}
    for p in periods:
        recs = by_period[p]
        if p in revised:
            kind = "revised"
            tip = _tip_records(cur, spec, p, tips, has_ed)
            found = period_problems(recs, tip, None, kind=kind)
            flips = zero_null_flips(recs, tip)
        else:
            kind = "new"
            tip = None
            earlier = [h for h in held if h < p]
            prev = (_tip_records(cur, spec, max(earlier), tips, has_ed)
                    if earlier else None)
            found = period_problems(recs, None, prev, kind=kind)
            flips = []
        hard = [x for x in found if x.startswith(PARTIAL)]
        soft = [x for x in found if not x.startswith(PARTIAL)]
        bad = list(hard)
        if kind == "revised":
            groups = classify_changes(recs, tip, p)
            print(f"  {p}: value changes against the held "
                  f"{'edition' if has_ed else 'live rows'}: {_counts(groups)}")
            if corr and p in ACKNOWLEDGED_CORRECTIONS[corr]["periods"]:
                why = correction_problems(corr, p, groups)
                if why:
                    bad.append(f"--acknowledge-correction {corr} does not "
                               "cover the changes: " + "; ".join(why))
                else:
                    a = ACKNOWLEDGED_CORRECTIONS[corr]
                    corrected[p] = f"{corr}: {_counts(groups)} ({a['decided']})"
                    print(f"  {p}: ACKNOWLEDGED (--acknowledge-correction): "
                          f"exactly the changes {corr} names "
                          f"({_counts(groups)}); {a['why']}")
                    soft, flips = [], []
        if flips:
            bad.append(f"{len(flips)} cell(s) go from 0 to NULL or NULL to 0 "
                       "against the held edition (rule 1.10), e.g. "
                       + ", ".join(f"{k} {c} {x}->{y}"
                                   for k, c, x, y in flips[:3])
                       + "; S13 releases a 0/NULL change only through a "
                       "named, decided correction (--acknowledge-correction "
                       "NAME; ACKNOWLEDGED_CORRECTIONS in the loader)")
        if soft and p in ack:
            acked[p] = "; ".join(soft)
            print(f"  {p}: ACKNOWLEDGED (--acknowledge): {acked[p]}")
        elif soft:
            bad += [x + f"; read the preview, then --acknowledge {p}"
                    for x in soft]
        if kind == "revised" and ranks.get(p) == rank and _differs(recs, tip):
            if p in reissue:
                reissued[p] = (f"--accept-reissue {p}: the files are "
                               f"{rank_text(rank)}, the same as the file of "
                               "the held tip, with different content (a "
                               "publisher reissue keeping its date)")
                print(f"  {p}: ACCEPTED REISSUE: {reissued[p]}")
            else:
                bad.append(f"the files are {rank_text(rank)}, the same as the "
                           "file of the held tip, but the content differs: "
                           "two files claim the same release. A publisher "
                           "reissue keeps its date: read the page's change "
                           f"note, then --accept-reissue {p}")
        if bad:
            problems[p] = bad
            continue
        notes = (older_note.get(p, ""), reissued.get(p, ""),
                 f"ACKNOWLEDGED {acked[p]}" if p in acked else "",
                 f"ACKNOWLEDGED {corrected[p]}" if p in corrected else "")
        how = (("new year" if kind == "new" else "restated year")
               + f" ({src['how']})")
        infos[p] = PeriodInfo(
            edition_source(ods, src, ods_sha, "; ".join(n for n in notes
                                                        if n)),
            _label(ods, src, sha, how, notes=notes), ((lsrc, sha),))
    for p, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {p}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [p for p in periods if p not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok and has_ed:
        def apply(c, prof_, per, rs, *, fetched_on, source_file=None):
            return _late("apply_year")(c, prof_, per, rs,
                                       fetched_on=fetched_on, info=infos[per])
        rc = pe.load_periods(cur, prof, ok, lambda per: by_period[per],
                             rank, args.commit, simulate=args.simulate,
                             against="editions", stats=stats, apply=apply)
    elif ok:
        live_prof = dataclasses.replace(prof, value_cols=VALUES)
        rc = pe.load_periods(cur, live_prof, ok, lambda per: by_period[per],
                             rank, False, against="live")
        print("  (no editions table yet: preview against live only, on the "
              "three live values)")
    if problems:
        print(f"REJECTED {len(problems)} year(s), nothing stored for them: "
              f"{', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        stored = live = 0  # rows written by this run; not a source value
        for p in stats["periods"]:
            k = stats["kinds"][p]
            if k in ("new", "revised"):
                stored += len(by_period[p])
            if k in ("new", LIVE_MISSING):
                live += len(by_period[p])
        left = [p for p in ok if p not in stats["periods"]]
        failed, not_tried = (left[:1], left[1:]) if left else ([], [])
        partial = ""
        if rc:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}"
                       + "".join(f" ({p}: {'; '.join(problems[p])[:300]})"
                                 for p in sorted(problems))
                       + f"; failed {failed or 'none'}; not attempted "
                       f"{not_tried or 'none'}; a year that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        rules = ", ".join(f"{a['rule']} {a['code']} {a['period']}"
                          for a in applied)
        notes = (partial + f"open data {src['where']} sha256 {sha[:16]} with "
                 f"LAHS {ods['year']} ODS sha256 {ods_sha[:16]}, "
                 f"{rank_text(rank)} ({src['how']}): "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(skipped)}" if skipped else "")
                 + (f"; zero rules: {rules}" if rules else "")
                 + "".join(f"; {older_note[p]}" for p in sorted(older_note)
                           if p in stats["periods"])
                 + "".join(f"; {reissued[p]}" for p in sorted(reissued)
                           if p in stats["periods"])
                 + "".join(f"; ACKNOWLEDGED {p}: {acked[p]}"
                           for p in sorted(acked) if p in stats["periods"])
                 + "".join(f"; ACKNOWLEDGED {corrected[p]} for {p}"
                           for p in sorted(corrected)
                           if p in stats["periods"])
                 + f". Rows stored: {stored} edition rows, {live} live rows.")
        log_run(cur, stored + live, notes, started)
        conn.commit()
        print("pipeline_run_log row written"
              + (" (partial run: see its notes)" if rc else ""))
    return rc


# ---------------------------------------------------------------------------
# One-off: migrate the held table
# ---------------------------------------------------------------------------

def live_state(cur, spec=None) -> tuple:
    """(rows, survey hash, distinct loaded_at, distinct years) of the live
    table. The survey hash is the spec's: md5 of lad24cd|reporting_year|
    households_on_register|jointly_managed_register|reasonable_preference|
    source (NULL as '~'), rows ordered by reporting_year, lad24cd, joined by
    LF."""
    spec = spec or globals()["SPEC"]
    cur.execute(f"""SELECT COUNT(*), md5(string_agg(coalesce(lad24cd::text,
        '~')||'|'||coalesce(reporting_year::text,'~')||'|'||
        coalesce(households_on_register::text,'~')||'|'||
        coalesce(jointly_managed_register::text,'~')||'|'||
        coalesce(reasonable_preference::text,'~')||'|'||
        coalesce(source::text,'~'), E'\\n' ORDER BY reporting_year,
        lad24cd)), COUNT(DISTINCT loaded_at), COUNT(DISTINCT reporting_year)
        FROM public.{spec.live_table}""")
    return tuple(cur.fetchone())


def _candidates(cur, od) -> dict:
    """{(lad24cd, period): [parsed rows in file order]} of a read file, codes
    resolved as load resolves them; halts on a cell or geography problem."""
    try:
        parsed = parse_rows(od["rows"])
    except ValueError as e:
        halt(f"{e}; nothing stored")
    out, problems = {}, []
    for p in sorted({r["period"] for r in parsed}):
        rows = [r for r in parsed if r["period"] == p]
        rmap, probs = resolve_codes(cur, {r["code"] for r in rows}, p,
                                    {r["code"]: r["lad24cd_pub"]
                                     for r in rows})
        problems += probs
        for r in rows:
            if r["code"] in rmap:
                out.setdefault((rmap[r["code"]], p), []).append(r)
    if problems:
        halt("geography check failed, nothing stored: "
             + "; ".join(dict.fromkeys(problems)))
    return out


def _n8n_view(r, period) -> tuple:
    """(households, cc2a, the expected live source, rule) of a source row
    as the n8n load stored it: no sums, [x] and [z] NULL, Yes/No as
    booleans; Telford and Wrekin's zeros from 2022 NULL with the note in
    source (set by direct SQL on 2026-09-30, the rule
    TELFORD_NO_REGISTER)."""
    h = r[HOUSEHOLDS]
    rule = None
    src = N8N_SOURCE.format(period)
    if _rule_fires(TELFORD_NO_REGISTER, r):
        h = None
        rule = TELFORD_NO_REGISTER
        src += f" [{TELFORD_NO_REGISTER['note']}]"
    return h, r[JOINTLY], src, rule


def migrate_legacy(cur, csv_feb, *, write, legacy=None, files=None,
                   june=_DEFAULT) -> dict:
    """One-off. Preconditions: on write the editions table and ledger exist
    and are empty (in a preview they may be absent; if present they must be
    empty); live as surveyed (legacy (rows, survey hash, loaded_at values,
    years), default LEGACY_LIVE); the file's sha256 as files['sha256']
    (default LEGACY_FILE). Proof, any failure halts: every live row is
    reproduced by the n8n rules (_n8n_view) from one source row of its key:
    the first row in file order where it is; another predecessor's row is
    recorded (n8n's DISTINCT ON kept an arbitrary one); rows loaded later
    than the n8n load (the three of 2026-08-20) from the June file (june,
    default LEGACY_JUNE, its sha256 checked); reasonable_preference NULL
    (cc5a was never loaded). Edition 1 'as loaded' of every year is live as
    held, with the editions-only columns from the matched row (value_flag
    the matched markers, the Telford rule, and reasonable_preference=
    not_loaded; return_status and predecessor_codes the matched row's;
    imputed_cc1a NULL, not checked; live_source the live source), and a
    ledger row per year for the file (outcome 'unchanged', edition 1, where
    files['where']) so a --file of it finds it checked. Live untouched.
    Nothing commits here; the caller rolls everything back on a halt."""
    g = globals()
    legacy = tuple(legacy or g["LEGACY_LIVE"])
    files = files or g["LEGACY_FILE"]
    june = g["LEGACY_JUNE"] if june is _DEFAULT else june
    prof = profile()
    spec = prof.spec
    lt = pe.file_checks_table(prof)
    ready = {spec.editions_table: table_exists(cur, spec.editions_table),
             lt: table_exists(cur, lt)}
    if write and not all(ready.values()):
        halt(f"missing {[t for t, ok in ready.items() if not ok]}; run "
             "`ddl --commit` first")
    for t, ok in ready.items():
        if ok:
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            n = cur.fetchone()[0]
            if n:
                halt(f"{t} already holds {n} rows; migrate-legacy runs once, "
                     "on empty tables")
    state = tuple(live_state(cur, spec))
    if state != legacy:
        halt(f"{spec.live_table} is not as surveyed: (rows, hash, loaded_at "
             f"values, years) {state}, expected {legacy}; nothing stored")
    path = Path(csv_feb)
    sha = content_sha256(path)
    if sha != files["sha256"]:
        halt(f"{path.name}: sha256 {sha[:16]}, expected "
             f"{files['sha256'][:16]} (the held file); nothing stored")
    rank = files.get("rank") or g["LEGACY_RANK"]
    od = _read_csv(path)
    cur.execute(f"SELECT {KEY}, {spec.period_col}, loaded_at FROM "
                f"public.{spec.live_table}")
    loaded = {(k, pe._p(p)): t for k, p, t in cur.fetchall()}
    first_load = min(loaded.values())
    later = sorted(k for k, t in loaded.items() if t != first_load)
    cand = _candidates(cur, od)
    cand_june = {}
    if later:
        if june is None:
            halt(f"{len(later)} live row(s) were loaded after the n8n load "
                 f"({', '.join(f'{k} {p}' for k, p in later[:6])}); the proof "
                 "needs the June 2026 file they came from (LEGACY_JUNE); "
                 "nothing stored")
        jsha = content_sha256(june["path"])
        if jsha != june["sha256"]:
            halt(f"{Path(june['path']).name}: sha256 {jsha[:16]}, expected "
                 f"{june['sha256'][:16]} (the June 2026 file); nothing stored")
        cand_june = _candidates(cur, _read_csv(june["path"]))
    periods = sorted({p for _, p in loaded})
    file_periods = sorted({p for _, p in cand})
    if periods != file_periods:
        halt(f"live years {periods}, the file's years {file_periods}; "
             "nothing stored")
    first, not_first, from_june, telford, diffs = 0, [], [], [], []
    plan = {"periods": {}, "not_first": [], "sha": sha}
    for p in periods:
        held = records(cur, spec, p)
        keys_live = {r[KEY] for r in held}
        keys_file = {k for k, q in cand if q == p}
        diffs += [("only held", k, p) for k in sorted(keys_live - keys_file)]
        diffs += [("only in the file", k, p)
                  for k in sorted(keys_file - keys_live)]
        out = []
        for r in sorted(held, key=lambda x: x[KEY]):
            k = r[KEY]
            if k not in keys_file:
                continue
            is_later = (k, p) in later
            rows = (cand_june if is_later else cand).get((k, p), [])
            hit = None
            for i, s in enumerate(rows):
                h, j, src, rule = _n8n_view(s, p)
                if (h, j, src) == (r[HOUSEHOLDS], r[JOINTLY],
                                   r["live_source"]) and \
                        _num(h) == _num(r[HOUSEHOLDS]) and \
                        r[REASONABLE] is None:
                    hit = (i, s, rule)
                    break
            if hit is None:
                diffs.append((k, p, (r[HOUSEHOLDS], r[JOINTLY],
                                     r[REASONABLE], r["live_source"]),
                              [_n8n_view(s, p)[:3] for s in rows][:4]))
                continue
            i, s, rule = hit
            if is_later:
                from_june.append((k, p))
            elif i == 0:
                first += 1
            else:
                not_first.append((k, p, i, s["code"]))
            if rule:
                telford.append((k, p))
            flags = {c: f for c, f in s["flags"].items()
                     if r[c] is None}
            if rule:
                flags[rule["column"]] = rule["flag"]
            flags[REASONABLE] = NOT_LOADED
            rec = dict(r)
            rec["value_flag"] = value_flag(rec, flags)
            rec["return_status"] = s["status"]
            rec["imputed_cc1a"] = None
            rec["predecessor_codes"] = s["code"]
            out.append(rec)
        dates = sorted({loaded[(r[KEY], p)] for r in held})
        pub = max(t.astimezone(timezone.utc).date() for t in dates)
        lbl = (core.AS_LOADED_LABEL if len({t.astimezone(timezone.utc).date()
                                            for t in dates}) == 1
               else core.AS_LOADED_LATEST_LABEL)
        sf = (f"as loaded: {files['name']}; LAHS {od['newest']}; latest update "
              f"{rank.isoformat()}; n8n run-log 28 (2026-04-01), one row per "
              "key, no sums, cc5a not loaded")
        jk = [k for k, q in from_june if q == p]
        if jk:
            sf += (f"; {len(jk)} row(s) set on 2026-08-20 from the June 2026 "
                   f"file (sha256 {june['sha256'][:16]}): {', '.join(jk)}")
        tk = [k for k, q in telford if q == p]
        if tk:
            sf += f"; {', '.join(tk)} NULL by TELFORD_NO_REGISTER (2026-09-30)"
        plan["periods"][p] = {"records": out, "published": pub, "label": lbl,
                              "source_file": sf,
                              "content_sha": rows_content_sha(out)}
    if diffs:
        halt(f"proof failed: {len(diffs)} difference(s) between the held rows "
             f"and the held file read with the n8n rules, e.g. {diffs[:5]}; "
             "nothing stored")
    plan["not_first"] = [(k, p) for k, p, _, _ in not_first]
    plan["rows"] = sum(len(v["records"]) for v in plan["periods"].values())
    plan["ledger"] = ledger_source(files["where"], rank, od["newest"],
                                   periods)
    print(f"proof: the held file read with the n8n rules reproduces every "
          f"held row: {plan['rows']:,} rows over {len(periods)} years; "
          f"{first:,} from the first row of their key in file order; "
          f"{len(not_first)} from another predecessor's row (n8n's DISTINCT "
          "ON kept an arbitrary one; edition 1 stores live as held): "
          + (", ".join(f"{k} {p} (row {i + 1}, {c})"
                       for k, p, i, c in not_first) or "none")
          + f"; {len(from_june)} from the June 2026 file (set 2026-08-20): "
          + (", ".join(f"{k} {p}" for k, p in from_june) or "none")
          + f"; {len(telford)} NULL by TELFORD_NO_REGISTER: "
          + (", ".join(f"{k} {p}" for k, p in telford) or "none")
          + "; 0 differences")
    for p, v in plan["periods"].items():
        print(f"  {p}: edition 1 as loaded, {len(v['records'])} rows, "
              f"published {v['published']} ({v['label']}); content sha256 "
              f"{v['content_sha']}")
    print(f"  ledger (each year): {plan['ledger']}")
    if not write:
        return plan
    cur.execute(f"SAVEPOINT {prof.savepoint}")
    for p, v in plan["periods"].items():
        ed = core.insert_edition(
            cur, spec, v["records"], p, release_label=v["label"],
            published_date=v["published"], source_file=v["source_file"],
            source_sha256=v["content_sha"], supersedes=None, strict=True)
        if ed != 1 or core.rows_differing(cur, spec, p, ed):
            halt(f"{spec.editions_table} {p}: edition 1 does not equal live; "
                 "rolled back")
        pe.record_file_check(cur, prof, p, plan["ledger"], sha, "unchanged", 1)
    cur.execute(f"RELEASE SAVEPOINT {prof.savepoint}")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, args.file, write=writing)
            if args.commit:
                log_run(cur, plan["rows"], f"migrate-legacy: edition 1 as "
                        f"loaded for {', '.join(plan['periods'])} "
                        f"({plan['rows']} rows) from the live table; proof: "
                        "the held file (sha256 "
                        f"{plan['sha'][:16]}) read with the n8n rules "
                        f"reproduces every held row ({len(plan['not_first'])} "
                        "from another predecessor's row); ledger rows for "
                        "the file. Live untouched.", started)
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
                  + f" as edition {new}; run refresh-latest to apply it")
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
        description="S13 MHCLG LAHS housing register: editions loader on the "
        "period-editions engine. There is no sync-new; migrate-legacy "
        "records the held years once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table, its triggers "
                       "and the file-check ledger (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="find the open data CSV and its newest "
                       "year's ODS, compare and store the years it states "
                       "(preview by default)")
    p.add_argument("--release", type=_label_arg, metavar="YYYY-YY",
                   help="the open data file must run to this LAHS year")
    p.add_argument("--file", metavar="PATH",
                   help="a local open data CSV (nothing downloaded)")
    p.add_argument("--ods", metavar="PATH",
                   help="with --file: the local year ODS (its identity)")
    p.add_argument("--no-page", action="store_true",
                   help="with --file and --ods: do not read the year page "
                   "(recorded as such)")
    p.add_argument("--recheck", type=_year_arg, metavar="YYYY",
                   help="compare this held year again although the file is "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a year whose tip comes from a "
                   "newer file, or a year before the newest held (logged)")
    p.add_argument("--acknowledge", action="append", type=_year_arg,
                   metavar="YYYY",
                   help="release a year's thresholds after reading the "
                   "preview (repeatable; logged; never a partial file or a "
                   "0/NULL change)")
    p.add_argument("--accept-reissue", action="append", type=_year_arg,
                   metavar="YYYY",
                   help="store a year whose files have the held rank but "
                   "different content (repeatable; logged)")
    p.add_argument("--acknowledge-correction", metavar="NAME",
                   choices=sorted(ACKNOWLEDGED_CORRECTIONS),
                   help="release exactly the changes a named, decided "
                   "correction lists (ACKNOWLEDGED_CORRECTIONS; logged)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each year's latest "
                       "edition into the live table, loaded_at included "
                       "(preview by default)")
    p.add_argument("--accept-drift", action="append", metavar="YYYY",
                   help="overwrite this year although its live rows equal no "
                   "stored edition (repeatable)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for the held years, with the proof (preview by "
                       "default)")
    p.add_argument("file", metavar="FILE")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("period", type=_year_arg, metavar="YYYY")
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
