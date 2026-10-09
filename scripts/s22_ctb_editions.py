"""S22 (MHCLG Council Taxbase and Live Table 615): edition history and loader
on the period-editions engine.

Three live tables, the latest layer each, keep their names, keys and readers:

    la_council_taxbase_empties  one row per billing authority and taxbase
                                year (W1 and v_la_empty_homes_rates read it)
    la_ctb_exemption_classes    the 11 unoccupied exemption classes (B, D-L,
                                Q) per authority and taxbase year
    la_vacant_dwellings_615     Live Table 615, one row per published code and
                                year where either sheet has a number

Two publishers' files: the Council Taxbase (CTB) release's local authority
level workbook (one taxbase year; re-issued as a revised file for the same
year) fills the first two; Live Table 615 (every year from 2004 in one file,
re-issued two or three times a year) fills the third. This module owns the
editions tables of all three, append-only, and two file-check ledgers:

    SPEC_CTB      la_council_taxbase_empties_editions      period taxbase_year
    SPEC_CLASSES  la_ctb_exemption_classes_editions        period taxbase_year
    SPEC_615      la_vacant_dwellings_615_editions          period year
    ledgers       la_council_taxbase_empties_editions_file_checks,
                  la_vacant_dwellings_615_editions_file_checks

An edition is what one file says about one period. The CTB main and classes
editions of a year are always stored together: one file, one savepoint, the
same edition number and source; if only one of the two differs from its tip
both get the next edition (their content hash is the pair's).

History (so the wording here stays truthful): the old build (scripts
s22_ctb_discover.py, s22_ctb_empties_build.py, s22_run.py, s22_verify.py,
committed together in 8c472a1) upserted by design (INSERT ... ON CONFLICT DO
UPDATE ... loaded_at = now()). The held data comes from its single run,
pipeline_run_log id 83 (status 'complete', 2026-08-13 01:18:50 to 01:25:04
UTC, 10,724 rows) and has not been rewritten since: every row of the three
tables carries one loaded_at. The committed code differs from what ran in at
least the run-log status. No earlier S22 load existed, so nothing was
overwritten in practice. The rule-1 defect of the old build (a suppressed or
blank class cell counted as 0 inside unoccupied_exemptions_total) never
fired on the held data: every one of the 11 class cells is a published
integer in the held 2025 workbook. migrate-legacy (one-off) records each held
period as edition 1 "as loaded", with a proof that this parser reproduces
every held cell from the held files.

Blanks and zeros (docs/RULES.md rule 1): [x] (not available or suppressed)
and [z] (not applicable) are NULL, never 0, with the reason in null_reasons.
A blank, [i] (imputed: not seen in these files), any other marker, a
negative or a non-integer halts, except the declared Table 615 cells in
NON_INTEGER_HELD, stored rounded half up as held. empty_under_6_months is
NULL unless both parts are published; unoccupied_exemptions_total is NULL
unless all 11 classes are published. 0 to NULL or NULL to 0 against the tip
needs --acknowledge PERIOD.

Identity (rule 3), from each file itself on every path: the CTB cover title
and publication line (first published, revised), the release page's year when
the page is read, each used block found by number AND title wording with a
Total column, the 11 classes in table 2.01, the England row; Table 615's cover
title and Latest Update, both sheets, a header of one snapshot date per year
in order. Inside each file the sum of the authorities equals the England row
for every used column and year, else halt. The rank of a file is its own
cover date (CTB revised or first published; 615 Latest Update).

Geography (rule 4): geography.resolve('22', ..., by_period=...) with the
codes that carry a number in each period ('mixed': one period never carries
both forms of Barnsley or Sheffield); a code in la_boundaries is 'direct', a
la_code_lookup recode 'resolved_via_lookup'; a published Table 615 district
the loader can show as abolished stays 'unmapped' with lad24cd NULL (never
mapped to a successor: a sum would double count); anything else is
UNEXPLAINED and stops.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
periods.
    python scripts/s22_ctb_editions.py ddl [--commit | --simulate]
    python scripts/s22_ctb_editions.py status
    python scripts/s22_ctb_editions.py load [--release YYYY] [--file PATH]
        [--recheck YEAR] [--allow-older-file] [--acknowledge YEAR ...]
        [--commit | --simulate]
    python scripts/s22_ctb_editions.py load-615 [--file PATH] [--recheck YEAR]
        [--allow-older-file] [--acknowledge YEAR ...] [--commit | --simulate]
        # the file is downloaded to data/raw/s22_ctb/ (also in a preview; a
        # preview writes nothing to the database). A period breaking a stop
        # condition is REJECTED (nothing stored, exit 1).
    python scripts/s22_ctb_editions.py refresh-latest [--part ctb|615]
        [--commit | --simulate]
    python scripts/s22_ctb_editions.py migrate-legacy CTB_FILE T615_FILE
        [--commit | --simulate]
    python scripts/s22_ctb_editions.py restore-edition ctb|615 PERIOD N
        [--commit | --simulate]
"""
import argparse
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
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "Source 22 - MHCLG Council Taxbase Empty Homes"   # run log id 83
RUN_SOURCE = "22"
LIVE_MISSING = pe.LIVE_MISSING

API = "https://www.gov.uk/api/content"
COLLECTION_PATH = "/government/collections/council-taxbase-statistics"
LIVE_TABLES_PATH = ("/government/statistical-data-sets/"
                    "live-tables-on-dwelling-stock-including-vacants")
GOV_UK = "https://www.gov.uk"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s22_ctb"            # git-ignored
USER_AGENT = ("ucws-pipeline S22 loader (read-only download of the public "
              "MHCLG Council Taxbase and Live Table 615 files)")
MIN_FILE_BYTES = 100 * 1024
ODS_MIME = b"application/vnd.oasis.opendocument.spreadsheet"

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-09)
# ---------------------------------------------------------------------------

CTB_VALUE_TYPES = (
    ("total_dwellings", "integer"),
    ("empty_under_6_months", "integer"),
    ("empty_6_months_plus", "integer"),
    ("empty_total", "integer"),
    ("empty_homes_premium_count", "integer"),
    ("second_homes", "integer"),
    ("unoccupied_exemptions_total", "integer"),
)
CTB_EXTRA_TYPES = (("la_name", "varchar(100)"), ("null_reasons", "text"))
CLASS_VALUE_TYPES = (("dwellings", "integer"),)
CLASS_EXTRA_TYPES = (("exemption_description", "text"),
                     ("null_reasons", "text"))
T615_VALUE_TYPES = (("vacant_dwellings", "integer"),
                    ("long_term_vacant_dwellings", "integer"))
T615_EXTRA_TYPES = (("published_la_name", "varchar(100)"),
                    ("lad24cd", "varchar(9)"),
                    ("mapping_status", "varchar(20) NOT NULL"),
                    ("null_reasons", "text"))


def _names(types) -> tuple:
    return tuple(c for c, _ in types)


CTB_VALUE_COLUMNS = _names(CTB_VALUE_TYPES)
CTB_DATA = CTB_VALUE_COLUMNS + _names(CTB_EXTRA_TYPES)
CLASS_DATA = _names(CLASS_VALUE_TYPES) + _names(CLASS_EXTRA_TYPES)
T615_VALUE_COLUMNS = _names(T615_VALUE_TYPES)
T615_DATA = T615_VALUE_COLUMNS + _names(T615_EXTRA_TYPES)
# the count columns a 0 <-> NULL flip is looked for in (rule 1.10)
FLIP_COLUMNS = CTB_VALUE_COLUMNS + ("dwellings",) + T615_VALUE_COLUMNS

# ---------------------------------------------------------------------------
# The CTB workbook
# ---------------------------------------------------------------------------

CTB_COVER_RE = re.compile(r"Council Taxbase: Local Authority Level Data for "
                          r"([0-9]{4})", re.I)
FIRST_RE = re.compile(r"\bon ([0-9]{1,2} [A-Za-z]+ [0-9]{4})")
REVISED_RE = re.compile(r"\brevised on ([0-9]{1,2} [A-Za-z]+ [0-9]{4})", re.I)
CTB_SHEET = "Council Taxbase Data"
SUPP_SHEET = "Supplementary Data"
# 1-based rows of both data sheets: block labels, column headers, England
LABEL_ROW, HEADER_ROW, ENGLAND_ROW = 5, 6, 7
ID_HEADERS = ("E-code", "ONS Code", "Region", "Local Authority")
ENGLAND = "E92000001"
DISTRICT_RE = re.compile(r"E0[6-9][0-9]{6}")
REGION_RE = re.compile(r"E12[0-9]{6}")
# (table number, published column, title wording that must be in the block's
# label). Found by number AND wording: a renumbered or retitled block halts.
CTB_BLOCKS = (
    ("1.01", "total_dwellings",
     re.compile(r"total number of dwellings on valuation list", re.I)),
    ("1.11", "second_homes", re.compile(r"classed as second homes", re.I)),
    ("1.17", "empty_homes_premium_count",
     re.compile(r"empty homes premium", re.I)),
    ("1.18", "empty_total",
     re.compile(r"total number of dwellings\b.*\bclassed as empty", re.I)),
    ("1.19", "empty_6_months_plus",
     re.compile(r"classed as empty\b.*\bmore than 6 months", re.I)),
)
CTB_PUBLISHED = tuple(f for _, f, _ in CTB_BLOCKS)
CLASS_BLOCK = ("2.01", re.compile(r"exempt classes", re.I))
# the unoccupied exemption classes: release Table 5a "Exemptions where the
# property is unoccupied (classes B, D - L, Q)"
UNOCCUPIED_CLASSES = ("B", "D", "E", "F", "G", "H", "I", "J", "K", "L", "Q")
CLASS_DESCRIPTIONS = {   # as held in la_ctb_exemption_classes
    "B": "Unoccupied dwelling owned by a charity (up to six months)",
    "D": "Unoccupied dwelling left empty by a person detained in prison or "
         "hospital",
    "E": "Unoccupied dwelling previously the sole residence of a person now "
         "in a care home or hospital",
    "F": "Unoccupied dwelling where the liable person is deceased "
         "(or probate granted less than six months ago)",
    "G": "Unoccupied dwelling whose occupation is prohibited by law",
    "H": "Unoccupied dwelling held for a minister of religion",
    "I": "Unoccupied dwelling left empty by a person receiving personal care "
         "elsewhere",
    "J": "Unoccupied dwelling left empty by a person providing personal care "
         "elsewhere",
    "K": "Unoccupied dwelling owned by a student and last occupied by "
         "students",
    "L": "Unoccupied dwelling where a mortgagee is in possession",
    "Q": "Unoccupied dwelling left empty by a bankrupt person's trustee",
}

# ---------------------------------------------------------------------------
# Live Table 615
# ---------------------------------------------------------------------------

T615_COVER_RE = re.compile(r"Live Table 615: Vacant Dwellings by Local "
                           r"Authority District, England, from 2004 to "
                           r"([0-9]{4})", re.I)
T615_SHEETS = (("All_vacants", "vacant_dwellings"),
               ("All_long_term_vacants", "long_term_vacant_dwellings"))
T615_HEADER_ROW = 3        # 1-based: 'ONS code', 'Area', then the dates
# Declared non-integer cells (both sheets): Dacorum E07000096 2012 is
# published as 1415.578 (all vacants) and 494.578 (long-term vacants); the
# England and East of England rows carry the same fraction; the file gives no
# reason. Held rounded half up (1416, 495). Any other non-integer halts.
NON_INTEGER_HELD = {("All_vacants", "E07000096", 2012),
                    ("All_long_term_vacants", "E07000096", 2012)}

MARKERS = {"[x]": "suppressed_or_not_available", "[z]": "not_applicable"}
BUILT_FROM_NULL = "built_from_null"

# ---------------------------------------------------------------------------
# Stop conditions (calibrated on the 2025 revision: 22 authorities corrected,
# England moved by less than 1%, publisher's change note)
# ---------------------------------------------------------------------------

CTB_AUTHORITIES = 296
CTB_NATIONAL_CHANGE = 0.02        # national total of any value column
CTB_CHANGED_AUTHORITIES = 30      # authorities changed in a revision
CTB_DWELLINGS_CHANGE = 0.10       # any authority's total_dwellings
T615_ENGLAND_CHANGE = 0.01        # England figure of either sheet
T615_BIG_CHANGE = 0.25            # an authority's value moving by more ...
T615_BIG_AUTHORITIES = 10         # ... in more than this many authorities
ABSENT_FROM_LOOKUP_SINCE = 2023   # a code absent from la_code_lookup with a
#                                   number in 2023 or later is UNEXPLAINED

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-09
# ---------------------------------------------------------------------------

# rows and md5 of every row as jsonb without loaded_at and null_reasons,
# ordered by key
LEGACY_LIVE = {
    "ctb": (296, "31ca63b26dc8041c" "ad663380dea77c66"),
    "classes": (3256, "983c3b130d7c6d5c" "878d9c85a82e73be"),
    "615": (7170, "27ffb076aa646811" "f998538d7745379f"),
}
# the two files the old build read (sha256 split so the credential scan does
# not flag them); the final URL each answers from today
LEGACY_FILES = {
    "ctb": {"name": "2025_Local_Authority_Drop_Down.xlsx",
            "sha256": "9fd74444f25278b53f7666dc23ff060d"
                      "1996adcfe27e74caf63aef2b5b19af06",
            "url": "https://assets.publishing.service.gov.uk/media/"
                   "696f605ff6aa424b452e3359/"
                   "2025_Local_Authority_Drop_Down.xlsx"},
    "615": {"name": "Live_Table_615.ods",
            "sha256": "16d404d2595e5e779afde9fb6b1f316a"
                      "47b58388be038838f257a09eb89f4dae",
            "url": "https://assets.publishing.service.gov.uk/media/"
                   "6a2bf816e50716856ed4afdd/Live_Table_615.ods"},
}
# before the editions exist (a preview against live): the held 615 rows came
# from the file whose cover says Latest Update 25 June 2026
LEGACY_615_RANK = date(2026, 6, 25)


# ---------------------------------------------------------------------------
# Discovery: the GOV.UK content API
# ---------------------------------------------------------------------------

DOC_TITLE_RE = re.compile(r"Council Taxbase ([0-9]{4})(?: in)? England"
                          r"( \(revised\))?")
LA_TITLE_RE = re.compile(r"Council Taxbase: Local authority level data for "
                         r"([0-9]{4})( \(revised\))?", re.I)
T615_TITLE_RE = re.compile(r"Table 615\b")


def _json(x):
    return json.loads(x) if isinstance(x, (str, bytes)) else (x or {})


def _norm(s) -> str:
    return " ".join(str(s or "").split())


def _documents(collection_json) -> list:
    d = _json(collection_json)
    return list(((d.get("links") or {}).get("documents")) or [])


def release_candidates(collection_json) -> dict:
    """{year: [(title, base_path)]} of the collection's documents titled
    'Council Taxbase <yyyy> in England' (a '(revised)' item included)."""
    out = {}
    for doc in _documents(collection_json):
        m = DOC_TITLE_RE.fullmatch(_norm(doc.get("title")))
        if m and doc.get("base_path"):
            out.setdefault(int(m.group(1)), []).append(
                (_norm(doc.get("title")), doc["base_path"]))
    return out


def latest_release(collection_json) -> tuple:
    """(year, base_path) of the newest 'Council Taxbase <yyyy> in England'
    release. A '(revised)' duplicate of an older year is ignored; two
    documents for the newest year raise ValueError listing them (no winner
    is guessed), as does a collection with none."""
    cands = release_candidates(collection_json)
    if not cands:
        raise ValueError("no 'Council Taxbase <yyyy> in England' document in "
                         "the collection; titles seen: "
                         f"{[_norm(d.get('title')) for d in _documents(collection_json)][:20]}")
    year = max(cands)
    if len(cands[year]) != 1:
        raise ValueError(f"{len(cands[year])} documents for the newest year "
                         f"{year}: {[t for t, _ in cands[year]]}; refusing "
                         "to choose one")
    return year, cands[year][0][1]


def release_for_year(collection_json, year) -> tuple:
    """(year, base_path) of the given year's release (--release YYYY);
    ValueError unless exactly one document is that year's."""
    cands = release_candidates(collection_json).get(int(year), [])
    if len(cands) != 1:
        raise ValueError(f"{len(cands)} documents for {year} in the "
                         f"collection: {[t for t, _ in cands]}")
    return int(year), cands[0][1]


def page_year(release_json) -> "int | None":
    m = DOC_TITLE_RE.fullmatch(_norm(_json(release_json).get("title")))
    return int(m.group(1)) if m else None


def _attachments(page_json) -> list:
    d = _json(page_json)
    return list(((d.get("details") or {}).get("attachments")) or [])


def la_attachment(release_json) -> dict:
    """{title, url, year, revised, content_type, file_size} of the release
    page's one attachment titled 'Council Taxbase: Local authority level data
    for <yyyy>' (with or without '(revised)'). ValueError listing every
    attachment title unless exactly one matches."""
    atts = _attachments(release_json)
    hits = [a for a in atts if LA_TITLE_RE.fullmatch(_norm(a.get("title")))]
    if len(hits) != 1 or not hits[0].get("url"):
        raise ValueError(f"{len(hits)} attachments titled like 'Council "
                         "Taxbase: Local authority level data for <yyyy>'; "
                         f"attachments: {[_norm(a.get('title')) for a in atts]}")
    a = hits[0]
    m = LA_TITLE_RE.fullmatch(_norm(a["title"]))
    return {"title": _norm(a["title"]), "url": a["url"],
            "year": int(m.group(1)), "revised": bool(m.group(2)),
            "content_type": a.get("content_type"),
            "file_size": a.get("file_size")}


def table_615_attachment(page_json) -> dict:
    """{title, url, content_type, file_size} of the live tables page's one
    attachment titled 'Table 615...'. ValueError listing the titles unless
    exactly one matches."""
    atts = _attachments(page_json)
    hits = [a for a in atts if T615_TITLE_RE.match(_norm(a.get("title")))]
    if len(hits) != 1 or not hits[0].get("url"):
        raise ValueError(f"{len(hits)} attachments titled 'Table 615...'; "
                         f"attachments: {[_norm(a.get('title')) for a in atts]}")
    a = hits[0]
    return {"title": _norm(a["title"]), "url": a["url"],
            "content_type": a.get("content_type"),
            "file_size": a.get("file_size")}


def change_history(page_json) -> list:
    """[(public_timestamp, note)] of the page, newest first."""
    d = _json(page_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    return [(str(c.get("public_timestamp") or "")[:10], _norm(c.get("note")))
            for c in ch]


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def cell(v) -> tuple:
    """(int, None) for a published whole number; (None, reason) for [x]
    (suppressed_or_not_available) or [z] (not_applicable). Anything else
    raises ValueError: a blank, [i] (imputed, not seen in these files), any
    other marker or text, a negative, or a non-integer."""
    if isinstance(v, bool):
        raise ValueError(f"value {v!r} is not a count")
    if isinstance(v, str):
        s = v.strip()
        if s in MARKERS:
            return None, MARKERS[s]
        if not s:
            raise ValueError("a blank cell (blanks halt; never read as 0)")
        if s == "[i]":
            raise ValueError("[i] (imputed): not seen in these files before; "
                             "halting rather than guessing how to store it")
        raise ValueError(f"unknown marker or text {v!r}")
    if v is None:
        raise ValueError("a blank cell (blanks halt; never read as 0)")
    if isinstance(v, numbers.Integral):
        v = int(v)
    elif isinstance(v, numbers.Real):
        if math.isnan(v):
            raise ValueError("a blank cell (blanks halt; never read as 0)")
        if not float(v).is_integer():
            raise ValueError(f"non-integer {v!r} (not a declared cell)")
        v = int(v)
    else:
        raise ValueError(f"value {v!r} ({type(v).__name__}) is not a count")
    if v < 0:
        raise ValueError(f"negative count {v}")
    return v, None


def half_up(v) -> int:
    """A declared non-integer rounded half up (1415.5 -> 1416)."""
    return int(Decimal(repr(float(v))).quantize(Decimal(1),
                                                 rounding=ROUND_HALF_UP))


def _text_date(s, *, month_only=False) -> date:
    s = _norm(s)
    fmt = "%B %Y" if month_only else "%d %B %Y"
    return datetime.strptime(s, fmt).date()


# ---------------------------------------------------------------------------
# Reading the CTB workbook
# ---------------------------------------------------------------------------

def _pad(row, width) -> tuple:
    row = tuple(row or ())
    return row + (None,) * (width - len(row))


def _sheet_rows(wb, name) -> list:
    ws = wb[name]
    rows = [tuple(r) for r in ws.iter_rows(values_only=True)]
    width = max((len(r) for r in rows), default=0)
    return [_pad(r, width) for r in rows]


def _blocks(label) -> list:
    """[(start, end, title)] of the table blocks on a label row."""
    starts = [i for i, v in enumerate(label)
              if v is not None and _norm(v)]
    return [(s, starts[k + 1] if k + 1 < len(starts) else len(label),
             _norm(label[s])) for k, s in enumerate(starts)]


def _find_block(blocks, number, wording, sheet) -> tuple:
    rx = re.compile(r"Table\s+" + re.escape(number) + r"(?![0-9])")
    hits = [b for b in blocks if rx.match(b[2])]
    if len(hits) != 1:
        raise ValueError(f"{sheet}: {len(hits)} blocks numbered Table "
                         f"{number}; titles seen: {[b[2][:60] for b in blocks][:40]}")
    if not wording.search(hits[0][2]):
        raise ValueError(f"{sheet}: Table {number} is titled "
                         f"{hits[0][2]!r}, which lacks the expected wording "
                         f"/{wording.pattern}/; the block map must be "
                         "corrected deliberately")
    return hits[0]


def _data_rows(rows, sheet) -> tuple:
    """(england row, {code: row}) of a data sheet. Raises on a missing or
    repeated England row, a duplicate code or an unknown code."""
    header = [_norm(v) for v in rows[HEADER_ROW - 1][:4]]
    if tuple(header) != ID_HEADERS:
        raise ValueError(f"{sheet}: identity columns {header}, expected "
                         f"{list(ID_HEADERS)}")
    england, out = None, {}
    for n, r in enumerate(rows[ENGLAND_ROW - 1:], start=ENGLAND_ROW):
        if all(v is None or not _norm(v) for v in r):
            continue
        code = _norm(r[1])
        if code == ENGLAND:
            if england is not None:
                raise ValueError(f"{sheet} row {n}: a second England row")
            england = r
            continue
        if not DISTRICT_RE.fullmatch(code):
            raise ValueError(f"{sheet} row {n}: code {code!r} is not an "
                             "E06-E09 authority code")
        if code in out:
            raise ValueError(f"{sheet} row {n}: duplicate code {code}; "
                             "refusing to choose a winner")
        out[code] = r
    if england is None:
        raise ValueError(f"{sheet}: no England row ({ENGLAND})")
    return england, out


def _cell_at(row, col, where):
    try:
        return cell(row[col])
    except ValueError as e:
        raise ValueError(f"{where}: {e}") from None


def read_ctb(path) -> dict:
    """The facts of one CTB local authority level workbook: {kind 'ctb',
    path, title, year, first_published, revised, rank, blocks {number:
    title}, england {column: (value, reason)}, england_classes {class:
    (value, reason)}, rows {code: {name, values {column: (value, reason)},
    classes {class: (value, reason)}}}}. rank is the cover's revised date,
    else its first published date. Raises ValueError (naming what was seen)
    on a missing sheet, a cover that is not the expected title and
    publication line, a block not found by number and title wording or
    without one Total column, a missing class, a missing England row, a
    duplicate or unknown code, codes differing between the two sheets, or a
    used cell that is blank, an unknown marker, negative or not an
    integer."""
    import openpyxl
    p = Path(path)
    try:
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001 (not a workbook)
        raise ValueError(f"{p.name}: not a readable .xlsx workbook ({e})") \
            from None
    try:
        missing = [s for s in ("Cover", CTB_SHEET, SUPP_SHEET)
                   if s not in wb.sheetnames]
        if missing:
            raise ValueError(f"{p.name}: sheets {missing} missing; sheets "
                             f"{wb.sheetnames}")
        cover = [_norm(r[0]) for r in _sheet_rows(wb, "Cover")
                 if r and r[0] is not None and _norm(r[0])]
        main = _sheet_rows(wb, CTB_SHEET)
        supp = _sheet_rows(wb, SUPP_SHEET)
    finally:
        wb.close()
    if len(cover) < 2:
        raise ValueError(f"{p.name}: the Cover sheet has no title and "
                         "publication line")
    m = CTB_COVER_RE.fullmatch(cover[0])
    if not m:
        raise ValueError(f"{p.name}: cover title {cover[0]!r}, expected "
                         "'Council Taxbase: Local Authority Level Data for "
                         "<yyyy>'")
    year = int(m.group(1))
    pub = cover[1]
    first = FIRST_RE.search(pub) if pub.lower().startswith("published") \
        else None
    if not first:
        raise ValueError(f"{p.name}: cover publication line {pub[:120]!r} "
                         "has no 'Published ... on <d Month yyyy>'")
    rev = REVISED_RE.search(pub)
    try:
        first_d = _text_date(first.group(1))
        rev_d = _text_date(rev.group(1)) if rev else None
    except ValueError as e:
        raise ValueError(f"{p.name}: cover dates unreadable ({e})") from None
    if len(main) < ENGLAND_ROW or len(supp) < ENGLAND_ROW:
        raise ValueError(f"{p.name}: the data sheets are too short")
    blocks = _blocks(main[LABEL_ROW - 1])
    header = main[HEADER_ROW - 1]
    cols, titles = {}, {}
    for number, field, wording in CTB_BLOCKS:
        s, e, title = _find_block(blocks, number, wording, CTB_SHEET)
        tot = [i for i in range(s, e) if _norm(header[i]) == "Total"]
        if len(tot) != 1:
            raise ValueError(f"{CTB_SHEET}: Table {number} has {len(tot)} "
                             "'Total' columns, expected one")
        cols[field], titles[number] = tot[0], title
    sblocks = _blocks(supp[LABEL_ROW - 1])
    sheader = supp[HEADER_ROW - 1]
    s, e, title = _find_block(sblocks, CLASS_BLOCK[0], CLASS_BLOCK[1],
                              SUPP_SHEET)
    titles[CLASS_BLOCK[0]] = title
    ccols = {}
    for i in range(s, e):
        mm = re.fullmatch(r"Class ([A-Z])", _norm(sheader[i]))
        if mm:
            if mm.group(1) in ccols:
                raise ValueError(f"{SUPP_SHEET}: Table 2.01 has Class "
                                 f"{mm.group(1)} twice")
            ccols[mm.group(1)] = i
    gone = [c for c in UNOCCUPIED_CLASSES if c not in ccols]
    if gone:
        raise ValueError(f"{SUPP_SHEET}: Table 2.01 lacks exemption classes "
                         f"{gone}; headers seen "
                         f"{[_norm(sheader[i]) for i in range(s, e)]}")
    eng, rows = _data_rows(main, CTB_SHEET)
    seng, srows = _data_rows(supp, SUPP_SHEET)
    if set(rows) != set(srows):
        raise ValueError(f"the two sheets list different authorities: only "
                         f"on {CTB_SHEET} {sorted(set(rows) - set(srows))[:6]}"
                         f", only on {SUPP_SHEET} "
                         f"{sorted(set(srows) - set(rows))[:6]}")
    out = {}
    for code in sorted(rows):
        r, sr = rows[code], srows[code]
        vals = {f: _cell_at(r, c, f"{code} {f}") for f, c in cols.items()}
        cls = {k: _cell_at(sr, c, f"{code} class {k}")
               for k, c in ccols.items() if k in UNOCCUPIED_CLASSES}
        name = _norm(r[3])
        if not name:
            raise ValueError(f"{code}: no authority name")
        out[code] = {"name": name, "values": vals, "classes": cls}
    return {"kind": "ctb", "path": p, "title": cover[0], "year": year,
            "first_published": first_d, "revised": rev_d,
            "rank": rev_d or first_d, "blocks": titles,
            "england": {f: _cell_at(eng, c, f"England {f}")
                        for f, c in cols.items()},
            "england_classes": {k: _cell_at(seng, ccols[k], f"England class {k}")
                                for k in UNOCCUPIED_CLASSES},
            "rows": out, "years": [year]}


def ctb_identity_problems(file, page_year=None, release=None) -> list:
    """Problems (empty = fine) between a read CTB workbook and where it came
    from: the release page's year (when the page was read) and --release
    must both equal the cover's year."""
    out = []
    if page_year is not None and page_year != file["year"]:
        out.append(f"the release page is {page_year}, the cover says "
                   f"{file['year']}")
    if release is not None and int(release) != file["year"]:
        out.append(f"--release {release}, the cover says {file['year']}")
    return out


# ---------------------------------------------------------------------------
# Reading Table 615
# ---------------------------------------------------------------------------

def _header_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _norm(v)
    try:
        return datetime.strptime(s, "%d/%m/%Y").date()
    except ValueError:
        raise ValueError(f"header {v!r} is not a dd/mm/yyyy snapshot date") \
            from None


def _blank(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or \
        (isinstance(v, str) and not v.strip())


def read_615(path) -> dict:
    """The facts of one Live Table 615 file (see parse_615). Reads the Cover
    sheet and the two loaded sheets (.ods through pandas and odfpy)."""
    import pandas as pd
    p = Path(path)
    names = ["Cover"] + [s for s, _ in T615_SHEETS]
    try:
        frames = pd.read_excel(p, sheet_name=names, header=None,
                               engine="odf" if p.suffix.lower() == ".ods"
                               else None)
    except Exception as e:  # noqa: BLE001 (missing sheet, not a file)
        raise ValueError(f"{p.name}: cannot read sheets {names} ({e})") \
            from None
    rows = {k: [list(r) for r in df.itertuples(index=False, name=None)]
            for k, df in frames.items()}
    out = parse_615(rows["Cover"], {s: rows[s] for s, _ in T615_SHEETS})
    out["path"] = p
    return out


def parse_615(cover_rows, sheets) -> dict:
    """{kind '615', title, end_year, latest_update, rank, dates {year:
    snapshot date}, years, england {sheet: {year: raw}}, rows {code: {name,
    vacant_dwellings {year: (value, reason)}, long_term_vacant_dwellings
    {...}, raw {sheet: {year: raw}}}}, non_integer [(sheet, code, year, raw,
    held)]}. rank: the cover's Latest Update ('25 June 2026', or 'January
    2026' read as 2026-01-01). Raises ValueError on a cover that is not the
    expected title, no Latest Update, a header that is not 'ONS code',
    'Area' then one snapshot date per year in order ending in the title's
    year, the two sheets differing in dates, codes or names, an unknown or
    duplicate code, or a district or England cell that is blank, [i], an
    unknown marker, negative or a non-integer outside NON_INTEGER_HELD."""
    cover = [_norm(r[0]) for r in cover_rows
             if r and not _blank(r[0])]
    if not cover:
        raise ValueError("the Cover sheet is empty")
    m = T615_COVER_RE.fullmatch(cover[0])
    if not m:
        raise ValueError(f"cover title {cover[0]!r}, expected 'Live Table "
                         "615: Vacant Dwellings by Local Authority District, "
                         "England, from 2004 to <yyyy>'")
    end_year = int(m.group(1))
    try:
        upd = cover[cover.index("Latest Update") + 1]
    except (ValueError, IndexError):
        raise ValueError("the cover has no 'Latest Update' date") from None
    try:
        rank = _text_date(upd)
    except ValueError:
        try:
            rank = _text_date(upd, month_only=True)
        except ValueError:
            raise ValueError(f"Latest Update {upd!r} is not a date") from None
    dates_by_sheet, data = {}, {}
    for sheet, field in T615_SHEETS:
        rows = sheets[sheet]
        if len(rows) < T615_HEADER_ROW:
            raise ValueError(f"{sheet}: no header row")
        head = rows[T615_HEADER_ROW - 1]
        if [_norm(v) for v in head[:2]] != ["ONS code", "Area"]:
            raise ValueError(f"{sheet}: header starts {head[:2]}, expected "
                             "'ONS code', 'Area'")
        dcols = [(i, _header_date(v)) for i, v in enumerate(head)
                 if i >= 2 and not _blank(v)]
        years = [d.year for _, d in dcols]
        if len(set(years)) != len(years):
            raise ValueError(f"{sheet}: two snapshot dates in one year "
                             f"({[d.isoformat() for _, d in dcols]})")
        if years != sorted(years):
            raise ValueError(f"{sheet}: snapshot dates out of order "
                             f"({[d.isoformat() for _, d in dcols]})")
        if not years or years[-1] != end_year:
            raise ValueError(f"{sheet}: the last snapshot year is "
                             f"{years[-1] if years else None}, the cover "
                             f"says to {end_year}")
        dates_by_sheet[sheet] = [d for _, d in dcols]
        data[sheet] = (dcols, rows[T615_HEADER_ROW:])
    first = T615_SHEETS[0][0]
    for sheet, _ in T615_SHEETS[1:]:
        if dates_by_sheet[sheet] != dates_by_sheet[first]:
            raise ValueError(f"{sheet} and {first} have different snapshot "
                             "dates")
    dates = {d.year: d for d in dates_by_sheet[first]}
    years = sorted(dates)
    out_rows, england, non_int = {}, {}, []
    # England carries a declared cell's fraction in that sheet and year
    declared_years = {(s, y) for s, _, y in NON_INTEGER_HELD}
    for sheet, field in T615_SHEETS:
        dcols, body = data[sheet]
        england[sheet] = {}
        seen = set()
        for n, r in enumerate(body, start=T615_HEADER_ROW + 1):
            if all(_blank(v) for v in r):
                continue
            code, name = _norm(r[0]), _norm(r[1])
            where = f"{sheet} row {n} {code}"
            if code in seen:
                raise ValueError(f"{where}: duplicate code; refusing to "
                                 "choose a winner")
            seen.add(code)
            if REGION_RE.fullmatch(code):
                continue           # regions are not stored or reconciled
            if code != ENGLAND and not DISTRICT_RE.fullmatch(code):
                raise ValueError(f"{where}: not England, a region or an "
                                 "E06-E09 district code")
            rec = None
            if code != ENGLAND:
                rec = out_rows.setdefault(code, {
                    "name": name, "raw": {},
                    **{f: {} for _, f in T615_SHEETS}})
                if rec["name"] != name:
                    raise ValueError(f"{where}: name {name!r} differs from "
                                     f"{rec['name']!r} on the other sheet")
                rec["raw"][sheet] = {}
            for i, d in dcols:
                v = r[i] if i < len(r) else None
                y = d.year
                declared = ((sheet, code, y) in NON_INTEGER_HELD
                            or (code == ENGLAND
                                and (sheet, y) in declared_years))
                if (declared and isinstance(v, numbers.Real)
                        and not isinstance(v, (bool, numbers.Integral))
                        and not math.isnan(v)
                        and not float(v).is_integer() and v >= 0):
                    val, why, raw = half_up(v), None, float(v)
                    if code != ENGLAND:
                        non_int.append((sheet, code, y, float(v), val))
                else:
                    try:
                        val, why = cell(v)
                    except ValueError as e:
                        raise ValueError(f"{where} {y}: {e}") from None
                    raw = val
                if code == ENGLAND:
                    england[sheet][y] = raw
                else:
                    rec[field][y] = (val, why)
                    rec["raw"][sheet][y] = raw
        if not england[sheet]:
            raise ValueError(f"{sheet}: no England row ({ENGLAND})")
    for code, rec in out_rows.items():
        gone = [s for s, _ in T615_SHEETS if s not in rec["raw"]]
        if gone:
            raise ValueError(f"{code} is missing from sheet(s) {gone}")
    return {"kind": "615", "title": cover[0], "end_year": end_year,
            "latest_update": upd, "rank": rank, "dates": dates,
            "years": years, "england": england, "rows": out_rows,
            "non_integer": non_int}


# ---------------------------------------------------------------------------
# Reconciliation inside a file
# ---------------------------------------------------------------------------

def reconcile(file) -> list:
    """Problems (empty = fine): for each used column (CTB) or sheet and year
    (615), the sum of the authorities' published values must equal the
    England row (exact; 615's declared non-integer cells compared unrounded,
    to within 1e-6)."""
    out = []
    if file["kind"] == "ctb":
        checks = [(f, file["england"][f],
                   [r["values"][f] for r in file["rows"].values()])
                  for f in CTB_PUBLISHED]
        checks += [(f"class {k}", file["england_classes"][k],
                    [r["classes"][k] for r in file["rows"].values()])
                   for k in UNOCCUPIED_CLASSES]
        for what, (eng, _), cells in checks:
            nulls = sum(1 for v, _ in cells if v is None)
            total = sum(v for v, _ in cells if v is not None)
            if eng is None or total != eng:
                out.append(f"{file['year']} {what}: authorities sum to "
                           f"{total:,} ({nulls} NULL), England "
                           f"{'NULL' if eng is None else f'{eng:,}'}")
        return out
    for sheet, field in T615_SHEETS:
        for y in file["years"]:
            eng = file["england"][sheet].get(y)
            vals = [r["raw"][sheet].get(y) for r in file["rows"].values()]
            total = sum(v for v in vals if v is not None)
            if eng is None:
                if any(v is not None for v in vals):
                    out.append(f"{sheet} {y}: England is not published but "
                               "authorities are")
                continue
            if not math.isclose(total, eng, rel_tol=0.0, abs_tol=1e-6):
                out.append(f"{sheet} {y}: authorities sum to {total:,}, "
                           f"England {eng:,}")
    return out


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def source_publication(file) -> str:
    """A CTB year's live source_publication and its edition's source_file,
    from the file's own cover and where it was read (the final URL, or the
    path of a --file): '<cover title>; first published <date>; revised
    <date>; <final URL>'."""
    parts = [file["title"], f"first published {file['first_published']}"]
    if file.get("revised"):
        parts.append(f"revised {file['revised']}")
    parts.append(str(file.get("source") or file.get("path")))
    return "; ".join(parts)


def edition_source_615(file) -> str:
    return (f"{file['title']}; Latest Update {file['rank']}; "
            f"{file.get('source') or file.get('path')}")


def _getter(resolve):
    return resolve if callable(resolve) else (lambda c: resolve[c])


def _reasons(rec, why, cols) -> "str | None":
    nulls = sorted(f"{c}={why[c]}" for c in cols if rec[c] is None)
    return ";".join(nulls) if nulls else None


def build_ctb(file, resolve) -> tuple:
    """(main_rows, class_rows) of a read CTB workbook (read_ctb). resolve:
    {publisher code: lad24cd} or a callable. Main rows: lad24cd, taxbase_year
    (string, the engine's period), the five published columns,
    empty_under_6_months (empty_total - empty_6_months_plus, NULL unless both
    are published), unoccupied_exemptions_total (the sum of the 11 classes,
    NULL unless all 11 are published), la_name, null_reasons and
    source_publication; class rows: lad24cd, taxbase_year, exemption_class,
    dwellings, exemption_description, null_reasons. null_reasons:
    'column=reason' for each NULL value (built_from_null for a derived
    column), sorted, ';'-joined, None when there is none. Two codes resolving
    to one lad24cd raise ValueError."""
    get = _getter(resolve)
    year = str(file["year"])
    src = source_publication(file)
    main, classes, seen = [], [], {}
    for code in sorted(file["rows"]):
        row = file["rows"][code]
        lad = get(code)
        if not lad:
            raise ValueError(f"{code} resolves to no lad24cd")
        if lad in seen:
            raise ValueError(f"{seen[lad]} and {code} both resolve to {lad}; "
                             "refusing to choose a winner")
        seen[lad] = code
        rec, why = {"lad24cd": lad, "taxbase_year": year}, {}
        for f in CTB_PUBLISHED:
            rec[f], why[f] = row["values"][f]
        et, e6 = rec["empty_total"], rec["empty_6_months_plus"]
        if et is None or e6 is None:
            rec["empty_under_6_months"] = None
            why["empty_under_6_months"] = BUILT_FROM_NULL
        else:
            if e6 > et:
                raise ValueError(f"{code}: empty_6_months_plus {e6} exceeds "
                                 f"empty_total {et}")
            rec["empty_under_6_months"] = et - e6
        parts = [row["classes"][k] for k in UNOCCUPIED_CLASSES]
        if all(v is not None for v, _ in parts):
            rec["unoccupied_exemptions_total"] = sum(v for v, _ in parts)
        else:
            rec["unoccupied_exemptions_total"] = None
            why["unoccupied_exemptions_total"] = BUILT_FROM_NULL
        rec["la_name"] = row["name"]
        rec["null_reasons"] = _reasons(rec, why, CTB_VALUE_COLUMNS)
        rec["source_publication"] = src
        main.append(rec)
        for k in UNOCCUPIED_CLASSES:
            v, r = row["classes"][k]
            classes.append({"lad24cd": lad, "taxbase_year": year,
                            "exemption_class": k, "dwellings": v,
                            "exemption_description": CLASS_DESCRIPTIONS[k],
                            "null_reasons": (f"dwellings={r}" if v is None
                                             else None)})
    main.sort(key=lambda r: r["lad24cd"])
    classes.sort(key=lambda r: (r["lad24cd"], r["exemption_class"]))
    return main, classes


def build_615(file, resolve) -> dict:
    """{year: [row]} of a read Table 615 file (parse_615): one row per
    published code and year where either sheet has a number (a row whose two
    cells are both markers is not stored). resolve: {code: (lad24cd or None,
    mapping_status)} or a callable. Row: published_la_code, year (string),
    vacant_dwellings, long_term_vacant_dwellings, published_la_name, lad24cd,
    mapping_status ('direct', 'resolved_via_lookup' or 'unmapped'),
    null_reasons."""
    get = _getter(resolve)
    out = {}
    for code in sorted(file["rows"]):
        rec = file["rows"][code]
        for y in file["years"]:
            cells = {f: rec[f][y] for _, f in T615_SHEETS}
            if all(v is None for v, _ in cells.values()):
                continue
            lad, status = get(code)
            row = {"published_la_code": code, "year": str(y),
                   "published_la_name": rec["name"], "lad24cd": lad,
                   "mapping_status": status}
            why = {}
            for f, (v, r) in cells.items():
                row[f], why[f] = v, r
            row["null_reasons"] = _reasons(row, why, T615_VALUE_COLUMNS)
            out.setdefault(y, []).append(row)
    return out


def codes_with_numbers(file) -> dict:
    """{year: {codes with at least one published number that year}}; a row
    of markers only is not a use of its code."""
    out = {}
    if file["kind"] == "ctb":
        out[file["year"]] = {
            c for c, r in file["rows"].items()
            if any(v is not None for v, _ in r["values"].values())
            or any(v is not None for v, _ in r["classes"].values())}
        return out
    for code, rec in file["rows"].items():
        for y in file["years"]:
            if any(rec[f][y][0] is not None for _, f in T615_SHEETS):
                out.setdefault(y, set()).add(code)
    return out


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def resolve_codes(cur, codes_by_period) -> tuple:
    """({code: (lad24cd or None, mapping_status)}, problems).
    geography.resolve(cur, '22', codes, by_period=codes_by_period) with the
    codes that carry a number in each period (source 22 is 'mixed': a period
    with both forms of Barnsley or Sheffield is a problem); then a code whose
    canonical form is in la_boundaries is 'direct' (or 'resolved_via_lookup'
    when recoded); a code the data shows abolished is 'unmapped' (lad24cd
    None): it has a la_code_lookup row (not 'current' or 'recode') and no
    number in any year from its abolition year on (abolitions take effect on
    1 April; the snapshot is in October or November), or it is absent from
    la_code_lookup and has no number from ABSENT_FROM_LOOKUP_SINCE on.
    Anything else is 'UNEXPLAINED' (a problem). Two codes reaching one
    lad24cd in one period are a problem."""
    by = {int(y): set(cs) for y, cs in (codes_by_period or {}).items()}
    codes = set().union(*by.values()) if by else set()
    recode, problems = geography.resolve(cur, RUN_SOURCE, codes, by_period=by)
    problems = list(problems)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, change_type, effective_date "
                "FROM public.la_code_lookup")
    abolished, listed = {}, set()
    for old, kind, eff in cur.fetchall():
        listed.add(old)
        if kind not in ("current", "recode") and eff is not None:
            y = eff.year
            abolished[old] = min(abolished.get(old, y), y)
    out = {}
    for c in sorted(codes):
        used = sorted(y for y, cs in by.items() if c in cs)
        r = geography.canonical(c, recode)
        if r in valid:
            out[c] = (r, "direct" if r == c else "resolved_via_lookup")
        elif c in abolished and not [y for y in used if y >= abolished[c]]:
            out[c] = (None, "unmapped")
        elif c not in listed and not [y for y in used
                                      if y >= ABSENT_FROM_LOOKUP_SINCE]:
            out[c] = (None, "unmapped")
        else:
            why = (f"abolished {abolished[c]} in la_code_lookup but has "
                   f"numbers in {[y for y in used if y >= abolished[c]]}"
                   if c in abolished else
                   f"not in la_boundaries or la_code_lookup and has numbers "
                   f"in {[y for y in used if y >= ABSENT_FROM_LOOKUP_SINCE]}"
                   if c not in listed else
                   "in la_code_lookup without an abolition date and not in "
                   "la_boundaries")
            problems.append(f"UNEXPLAINED {c}: {why}")
    for y, cs in sorted(by.items()):
        hit = {}
        for c in sorted(cs):
            lad = out.get(c, (None, None))[0]
            if lad:
                hit.setdefault(lad, []).append(c)
        two = {k: v for k, v in hit.items() if len(v) > 1}
        if two:
            problems.append(f"{y}: codes {two} reach one lad24cd; refusing "
                            "to choose a winner")
    return out, problems


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_FILE_DATED = re.compile(r"\bfile dated ([0-9]{4}-[0-9]{2}-[0-9]{2})\b")
_ISO = re.compile(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b")
_LEDGER_RE = re.compile(r"(.*) \(file dated ([0-9]{4}-[0-9]{2}-[0-9]{2}); "
                        r"years ([0-9]{4})(?:-([0-9]{4}))?\)")


def release_rank(source_file) -> "date | None":
    """The date of the file an edition (or a ledger row) came from, read
    from its source_file: the 'file dated YYYY-MM-DD' it carries, else the
    latest ISO date in it (a CTB source_publication: the cover's revised
    date); None if it names none."""
    s = str(source_file or "")
    m = _FILE_DATED.search(s)
    if m:
        return date.fromisoformat(m.group(1))
    found = [date.fromisoformat(x) for x in _ISO.findall(s)]
    return max(found) if found else None


def ledger_source(where, rank, years) -> str:
    """A file's ledger source_file: where it was read (the final URL, or the
    path of a --file), its own date and the periods it covers, so a later
    run knows the file's periods without reading it."""
    ys = sorted(int(y) for y in years)
    span = f"{ys[0]}" if ys[0] == ys[-1] else f"{ys[0]}-{ys[-1]}"
    return f"{where} (file dated {rank.isoformat()}; years {span})"


def parse_ledger_source(s) -> "tuple | None":
    """(where, rank, [years]) of a ledger source_file, or None."""
    m = _LEDGER_RE.fullmatch(str(s or ""))
    if not m:
        return None
    a = int(m.group(3))
    b = int(m.group(4)) if m.group(4) else a
    return m.group(1), date.fromisoformat(m.group(2)), list(range(a, b + 1))


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held period: the newest file date that stated it}: the tip
    edition's rank, or a later file the ledger records for the period (a
    file that found the period unchanged stores no edition)."""
    out = {}
    for y, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(y, ())]
        ranks = [r for r in ranks if r is not None]
        out[y] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, allow_older=False,
                    stranded=()) -> "list | None":
    """The periods the ledger records this file's (where, sha256) pair for,
    if that covers every period the file needs; else None (the file is read
    and each period planned). A file needs each period its ledger source
    declares (CTB: its one year; 615: every year of its header), except a
    held period whose tip comes from a newer file (skipped as older, unless
    allow_older); a stranded period (editions, no live rows) always makes
    the file be read. So a run that failed or was rejected part-way leaves
    a period without a ledger row and the rerun reads the file again."""
    have, declared = set(), None
    for y, pairs in checked.items():
        for s, h in pairs:
            parsed = parse_ledger_source(s)
            if h == sha and parsed and parsed[0] == where:
                have.add(int(y))
                declared = parsed
    if declared is None:
        return None
    _, rank, need = declared
    need = set(need)
    if not allow_older:
        need = {y for y in need if not (ranks.get(y) is not None
                                        and ranks[y] > rank)}
    if not need or need & set(stranded):
        return None
    return sorted(have) if need <= have else None


def plan_periods(periods, rank, ranks, checked, source, sha, *, recheck,
                 allow_older) -> tuple:
    """(new, revised, skipped). periods: the file's periods; rank: its own
    date; ranks: {held period: tip_ranks}; checked: {period: {(source,
    sha)}} of the ledger (stranded periods left out by the caller, so they
    are always read); source / sha: this file's ledger source and sha256.
    new: periods not held; revised: held periods to compare with their tip;
    skipped: {period: reason} for held periods whose tip comes from a newer
    file (unless allow_older: the older-file guard, per period, on every
    path) or whose (source, sha) the ledger already records (unless
    --recheck names it). --recheck PERIOD must be a held period of the file
    (ValueError)."""
    periods = sorted(int(p) for p in periods)
    if recheck is not None and (recheck not in periods or recheck not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held period of this file "
                         f"({periods[0]}-{periods[-1]})")
    new, revised, skipped = [], [], {}
    for y in periods:
        if y not in ranks:
            new.append(y)
            continue
        t = ranks[y]
        if t is not None and t > rank and not allow_older:
            skipped[y] = (f"older: its tip comes from a file dated {t}, this "
                          f"file is dated {rank}")
            continue
        if recheck != y and (source, sha) in checked.get(y, ()):
            skipped[y] = "checked: this file is already in the ledger"
            continue
        revised.append(y)
    return new, revised, skipped


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def _rkey(r):
    if "exemption_class" in r:
        return (r["lad24cd"], r["exemption_class"])
    if "published_la_code" in r:
        return r["published_la_code"]
    return r["lad24cd"]


def _by_key(records) -> dict:
    return {_rkey(r): r for r in records or ()}


def _pct(o, n) -> float:
    if o == n:
        return 0.0
    return math.inf if o == 0 else abs(n - o) / o


def _both_sums(keys, tk, nk, c) -> tuple:
    """(old, new) totals of column c over the keys published on both sides
    ((None, None) if there are none): a value going to or from NULL is the
    0/NULL rule's business, not a move in the total."""
    both = [k for k in keys if tk[k].get(c) is not None
            and nk[k].get(c) is not None]
    if not both:
        return None, None
    return sum(tk[k][c] for k in both), sum(nk[k][c] for k in both)


def period_problems(new, tip, *, kind, part, boundaries=None) -> list:
    """The stop conditions of one period (empty = fine). new: the period's
    records from the file (CTB: the main rows); tip: for kind 'revised' the
    tip edition's records of the period, for kind 'new' those of the held
    latest period (or empty). CTB, both kinds: not CTB_AUTHORITIES
    authorities, or an authority of the tip missing. CTB revised: a national
    total of any value column moving by more than CTB_NATIONAL_CHANGE, more
    than CTB_CHANGED_AUTHORITIES authorities changed, or any authority's
    total_dwellings moving by more than CTB_DWELLINGS_CHANGE. 615 new: an
    authority in la_boundaries (boundaries, after resolution) without a
    number on both sheets. 615 revised: a published code appearing or
    disappearing, the England figure of either sheet moving by more than
    T615_ENGLAND_CHANGE, or more than T615_BIG_AUTHORITIES authorities
    changing by more than T615_BIG_CHANGE. National and England totals are
    summed over the authorities published on both sides (equal to the
    England row by reconcile when nothing is NULL); a value going to or from
    NULL is the 0/NULL rule's business (zero_null_flips)."""
    g = globals()
    out = []
    nk, tk = _by_key(new), _by_key(tip)
    if part == "ctb":
        want = g["CTB_AUTHORITIES"]
        if len(nk) != want:
            out.append(f"{len(nk)} authorities, expected {want}")
        gone = sorted(set(tk) - set(nk))
        if gone:
            out.append(f"{len(gone)} authorit(ies) of the held period missing "
                       f"from the file: {', '.join(gone[:8])}")
        if kind != "revised":
            return out
        common = sorted(set(nk) & set(tk))
        for c in CTB_VALUE_COLUMNS:
            o, n = _both_sums(common, tk, nk, c)
            if o is not None and _pct(o, n) > g["CTB_NATIONAL_CHANGE"]:
                out.append(f"national {c} {o:,} -> {n:,} (limit "
                           f"{g['CTB_NATIONAL_CHANGE']:.0%})")
        changed = [k for k in common if any(nk[k].get(c) != tk[k].get(c)
                                            for c in CTB_VALUE_COLUMNS)]
        if len(changed) > g["CTB_CHANGED_AUTHORITIES"]:
            out.append(f"{len(changed)} authorities changed (limit "
                       f"{g['CTB_CHANGED_AUTHORITIES']})")
        big = []
        for k in common:
            o, n = tk[k].get("total_dwellings"), nk[k].get("total_dwellings")
            if o is not None and n is not None and o != n and \
                    _pct(o, n) > g["CTB_DWELLINGS_CHANGE"]:
                big.append(f"{k} {o:,}->{n:,}")
        if big:
            out.append(f"total_dwellings moves by more than "
                       f"{g['CTB_DWELLINGS_CHANGE']:.0%} in {len(big)} "
                       f"authorit(ies): {', '.join(big[:6])}")
        return out
    if kind == "new":
        have = {r["lad24cd"] for r in nk.values() if r.get("lad24cd")
                and all(r.get(c) is not None for c in T615_VALUE_COLUMNS)}
        miss = sorted(set(boundaries or ()) - have)
        if miss:
            out.append(f"{len(miss)} authorit(ies) in la_boundaries without a "
                       f"number on both sheets: {', '.join(miss[:8])}")
        return out
    appear, gone = sorted(set(nk) - set(tk)), sorted(set(tk) - set(nk))
    if appear:
        out.append(f"published code(s) appearing: {', '.join(appear[:8])}")
    if gone:
        out.append(f"published code(s) disappearing: {', '.join(gone[:8])}")
    common = sorted(set(nk) & set(tk))
    for c in T615_VALUE_COLUMNS:
        o, n = _both_sums(common, tk, nk, c)
        if o is not None and _pct(o, n) > g["T615_ENGLAND_CHANGE"]:
            out.append(f"England {c} {o:,} -> {n:,} (limit "
                       f"{g['T615_ENGLAND_CHANGE']:.0%})")
    big = []
    for k in sorted(set(nk) & set(tk)):
        for c in T615_VALUE_COLUMNS:
            o, n = tk[k].get(c), nk[k].get(c)
            if o is not None and n is not None and o != n and \
                    _pct(o, n) > g["T615_BIG_CHANGE"]:
                big.append(f"{k} {c} {o:,}->{n:,}")
                break
    if len(big) > g["T615_BIG_AUTHORITIES"]:
        out.append(f"{len(big)} authorities change by more than "
                   f"{g['T615_BIG_CHANGE']:.0%} (limit "
                   f"{g['T615_BIG_AUTHORITIES']}): {', '.join(big[:6])}")
    return out


def _flips(new, tip) -> list:
    nk, tk = _by_key(new), _by_key(tip)
    out = []
    for k in sorted(set(nk) & set(tk), key=str):
        for c in FLIP_COLUMNS:
            if c not in nk[k] and c not in tk[k]:
                continue
            a, b = tk[k].get(c), nk[k].get(c)
            if (a == 0 and b is None) or (a is None and b == 0):
                out.append((k, c, a, b))
    return out


def zero_null_flips(new, tip) -> int:
    """Cells (common keys, count columns) going from 0 to NULL or NULL to 0
    against the tip (rule 1.10)."""
    return len(_flips(new, tip))


def _differs(new, old, cols) -> bool:
    nk, ok = _by_key(new), _by_key(old)
    if set(nk) != set(ok):
        return True
    return any(nk[k].get(c) != ok[k].get(c) for k in nk for c in cols)


def cross_check_615_ctb(rows615, ctb_main, year) -> list:
    """Problems (empty = fine): 615's all-vacants of `year` must equal CTB
    empty_total + unoccupied_exemptions_total (615's cover: October 2025
    all vacants are CTB Line 15 plus classes B, D-L and Q, tables 1.18 and
    2.01) for every authority, by lad24cd."""
    a = {r["lad24cd"]: r.get("vacant_dwellings") for r in rows615
         if str(r.get("year")) == str(year) and r.get("lad24cd")}
    b = {r["lad24cd"]: (r.get("empty_total"),
                        r.get("unoccupied_exemptions_total"))
         for r in ctb_main if str(r.get("taxbase_year")) == str(year)}
    out = []
    for k in sorted(set(a) ^ set(b)):
        out.append(f"{year} {k}: only in {'Table 615' if k in a else 'CTB'}")
    for k in sorted(set(a) & set(b)):
        v, (e, u) = a[k], b[k]
        if v is None or e is None or u is None:
            out.append(f"{year} {k}: a NULL on one side, not compared")
        elif v != e + u:
            out.append(f"{year} {k}: 615 all vacants {v:,}, CTB empty_total "
                       f"{e:,} + unoccupied exemptions {u:,} = {e + u:,}")
    return out


# ---------------------------------------------------------------------------
# Specs and profiles
# ---------------------------------------------------------------------------

def tip_row_count(editions_table: str, period_col: str):
    """expected_rows_per_period for status: the tip edition's row count (a
    615 year's codes are its own)."""
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


SPEC_CTB = core.EditionSpec(
    name="s22_ctb",
    live_table="la_council_taxbase_empties",
    editions_table="la_council_taxbase_empties_editions",
    key_cols=("lad24cd",),
    period_col="taxbase_year",
    value_cols=CTB_VALUE_TYPES,
    extra_cols=CTB_EXTRA_TYPES,
    refresh_cols=CTB_DATA + ("source_publication", "loaded_at"),
    refresh_from=(("source_publication", "source_file"),
                  ("loaded_at", "loaded_at")),
    as_loaded_source_col="source_publication",
    refresh_source_whole_period=True,   # one source per refreshed year
    key_types=(("lad24cd", "varchar(9) NOT NULL"),
               ("taxbase_year", "integer NOT NULL")),
    fk_la_boundaries=True,
)
SPEC_CLASSES = core.EditionSpec(
    name="s22_classes",
    live_table="la_ctb_exemption_classes",
    editions_table="la_ctb_exemption_classes_editions",
    key_cols=("lad24cd", "exemption_class"),
    period_col="taxbase_year",
    value_cols=CLASS_VALUE_TYPES,
    extra_cols=CLASS_EXTRA_TYPES,
    refresh_cols=CLASS_DATA + ("loaded_at",),
    refresh_from=(("loaded_at", "loaded_at"),),
    key_types=(("lad24cd", "varchar(9) NOT NULL"),
               ("exemption_class", "varchar(2) NOT NULL"),
               ("taxbase_year", "integer NOT NULL")),
    fk_la_boundaries=True,
)
SPEC_615 = core.EditionSpec(
    name="s22_615",
    live_table="la_vacant_dwellings_615",
    editions_table="la_vacant_dwellings_615_editions",
    key_cols=("published_la_code",),
    period_col="year",
    value_cols=T615_VALUE_TYPES,
    extra_cols=T615_EXTRA_TYPES,
    refresh_cols=T615_DATA + ("loaded_at",),
    refresh_from=(("loaded_at", "loaded_at"),),
    key_types=(("published_la_code", "varchar(9) NOT NULL"),
               ("year", "integer NOT NULL")),
    fk_la_boundaries=False,     # lad24cd is not a key and may be NULL
    expected_rows_per_period=tip_row_count(
        "la_vacant_dwellings_615_editions", "year"),
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S22 release label comes from the file; store "
                     "through apply_ctb_year(...) or apply_615_year(...)")


def check_ctb_records(records, period) -> None:
    """ValueError unless the year's main records are whole: each of this
    year, one per lad24cd."""
    _check_unique(records, period, "taxbase_year")


def check_615_records(records, period) -> None:
    _check_unique(records, period, "year")


def _check_unique(records, period, pc) -> None:
    seen = set()
    for r in records:
        if r.get(pc) != period:
            raise ValueError(f"{period}: a record of {pc} {r.get(pc)!r}")
        k = _rkey(r)
        if k in seen:
            raise ValueError(f"{period}: {k} appears twice")
        seen.add(k)


PROFILE_CTB = pe.Profile(
    spec=SPEC_CTB, value_cols=CTB_DATA, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S22 Council Taxbase (la_council_taxbase_"
    "empties)", default_source_file="MHCLG Council Taxbase",
    expected_areas=CTB_AUTHORITIES, release_label=_no_label,
    check_records=check_ctb_records, file_checks=True, savepoint="s22_year",
    example_label="(CTB values)")
PROFILE_CLASSES = pe.Profile(
    spec=SPEC_CLASSES, value_cols=CLASS_DATA, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S22 exemption classes (la_ctb_exemption_"
    "classes)", default_source_file="MHCLG Council Taxbase",
    expected_areas=CTB_AUTHORITIES, release_label=_no_label,
    file_checks=False, savepoint="s22_year", example_label="(dwellings)")
PROFILE_615 = pe.Profile(
    spec=SPEC_615, value_cols=T615_DATA, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S22 Live Table 615 (la_vacant_dwellings_"
    "615)", default_source_file="MHCLG Live Table 615",
    expected_areas=None, release_label=_no_label,
    check_records=check_615_records, file_checks=True,
    savepoint="s22_615_year", example_label="(all vacants, long-term)")
PARTS = ("ctb", "615")


def profiles(part) -> tuple:
    """The part's profiles bound to the current specs (looked up when
    called, so tests can swap the specs): ctb (main, classes); 615 (615,)."""
    g = globals()
    if part == "ctb":
        n = g["CTB_AUTHORITIES"]
        return (dataclasses.replace(g["PROFILE_CTB"], spec=g["SPEC_CTB"],
                                    expected_areas=n),
                dataclasses.replace(g["PROFILE_CLASSES"],
                                    spec=g["SPEC_CLASSES"], expected_areas=n))
    if part == "615":
        return (dataclasses.replace(g["PROFILE_615"], spec=g["SPEC_615"]),)
    raise ValueError(f"part must be one of {PARTS}, not {part!r}")


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s22_ctb_editions, name, ...) reaches the
    engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur, specs=None) -> None:
    """ddl: the engine's editions table and triggers for the three specs,
    the two file-check ledgers (CTB main, 615) and the live column
    null_reasons on the three live tables (additive, NULL until a later
    edition is refreshed). Idempotent."""
    specs = specs or (SPEC_CTB, SPEC_CLASSES, SPEC_615)
    for s in specs:
        core.create_schema(cur, s)
        cur.execute(f"ALTER TABLE public.{s.live_table} "
                    "ADD COLUMN IF NOT EXISTS null_reasons text")
    for s in specs:
        prof = _profile_of(s)
        if prof.file_checks:
            pe.create_file_checks(cur, prof)


def _profile_of(spec) -> pe.Profile:
    """The profile of a spec (the real or a throwaway copy): the one with
    its key and period columns."""
    for prof in (PROFILE_CTB, PROFILE_CLASSES, PROFILE_615):
        if spec.period_col == prof.spec.period_col and \
                spec.key_cols == prof.spec.key_cols:
            return prof.with_spec(spec)
    raise ValueError(f"no profile for {spec.name}")


def insert_live(cur, profile, period: str, records: list) -> None:
    """A new period's live rows: the key, the period, the profile's value
    columns (null_reasons and the name columns included) and, for the CTB
    main table, source_publication (each record's); loaded_at takes its
    default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    extra = ((spec.as_loaded_source_col,)
             if spec.as_loaded_source_col == "source_publication" else ())
    cols = (tuple(spec.key_cols) + (spec.period_col,)
            + tuple(profile.value_cols) + extra)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r.get(c) for c in profile.value_cols)
         + tuple(r[c] for c in extra) for r in records],
        page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a period's edition records: source_file (CTB: the year's
    source_publication; 615: cover title, Latest Update, where read), the
    release label, and the file read ((ledger source, sha256),)."""
    source_file: str
    label: str
    files: tuple


def pair_sha(main, classes, spec_main=None, spec_cls=None) -> str:
    """The content hash of a CTB year's two editions: sha256 of the main
    and classes rows_sha256, so a change in either gives both a new
    edition."""
    a = core.rows_sha256(spec_main or SPEC_CTB, main)
    b = core.rows_sha256(spec_cls or SPEC_CLASSES, classes)
    return hashlib.sha256(f"{a}\n{b}".encode("ascii")).hexdigest()


def pair_kind(km, kc, period="") -> str:
    """The kind of a CTB year from its main and classes kinds: equal kinds
    stand; 'revised' in either makes both 'revised' (they stay paired); a
    'new' beside anything else halts (the two editions tables are out of
    step); unchanged beside live-missing is live-missing."""
    if km == kc:
        return km
    if "new" in (km, kc):
        halt(f"{period}: the main table is {km} but the classes are {kc}; "
             "the two editions tables are out of step; nothing stored")
    if "revised" in (km, kc):
        return "revised"
    return LIVE_MISSING


def _forced(kind):
    return lambda *a, **kw: kind


def apply_ctb_year(cur, prof_main, prof_cls, period, main, classes, *,
                   fetched_on, info) -> str:
    """Both specs' pe.apply_period for one taxbase year, then the ledger
    row, inside the caller's per-period savepoint (s22_year): a new year's
    edition 1 (main and classes), live rows and ledger row commit or roll
    back together. If either part differs from its tip both store the next
    edition; the two tips must then carry the same edition number."""
    check_records = prof_cls.check_records or (
        lambda r, p: _check_unique(r, p, prof_cls.spec.period_col))
    check_records(classes, period)
    km = pe.classify_period(cur, prof_main, period, main)
    kc = pe.classify_period(cur, prof_cls, period, classes)
    kind = pair_kind(km, kc, period)
    if kind == "revised":
        km = kc = "revised"
    sha = pair_sha(main, classes, prof_main.spec, prof_cls.spec)
    pm = dataclasses.replace(prof_main, release_label=lambda d: info.label,
                             content_sha256=lambda r: sha)
    pc = dataclasses.replace(prof_cls, release_label=lambda d: info.label,
                             content_sha256=lambda r: sha)
    pe.apply_period(cur, pm, period, main, fetched_on=fetched_on,
                    source_file=info.source_file, classify=_forced(km),
                    insert=_late("insert_live"))
    pe.apply_period(cur, pc, period, classes, fetched_on=fetched_on,
                    source_file=info.source_file, classify=_forced(kc),
                    insert=_late("insert_live"))
    tm = core.chain_tip(cur, pm.spec, period)
    tc = core.chain_tip(cur, pc.spec, period)
    if tm != tc:
        halt(f"{period}: main tip edition {tm}, classes tip edition {tc}; "
             "they must carry the same number; rolled back")
    for src, file_sha in info.files:
        pe.record_file_check(cur, pm, period, src, file_sha, kind, tm)
    return kind


def apply_615_year(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for one 615 year, then its ledger row, inside the
    caller's per-period savepoint (s22_615_year)."""
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
                           source_file=info.source_file,
                           insert=_late("insert_live"))
    tip = core.chain_tip(cur, prof.spec, period)
    for src, file_sha in info.files:
        pe.record_file_check(cur, prof, period, src, file_sha, kind, tip)
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


def _opens(path, suffix) -> None:
    """Raise unless the file opens as the expected format."""
    if suffix == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        try:
            if not wb.sheetnames:
                raise ValueError("a workbook with no sheets")
        finally:
            wb.close()
        return
    with zipfile.ZipFile(path) as z:
        if z.read("mimetype").strip() != ODS_MIME:
            raise ValueError("not an OpenDocument spreadsheet")
        z.getinfo("content.xml")


def fetch(url, dest, session=None) -> tuple:
    """Download url into the directory dest (redirects followed) and return
    (path to read, final URL). The file name is the final URL's. Checked
    before it is kept: more than MIN_FILE_BYTES, an .xlsx or .ods name, and
    it opens as that format. A same-named file with the same content is kept
    as it is; one with different content is never replaced: the download is
    saved beside it as <stem>-<sha8><suffix> and that is the one read. An
    existing name is never a reason to skip the download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    if len(body) <= MIN_FILE_BYTES:
        halt(f"{final}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written; saw {body[:100]!r}")
    name = Path(urlparse(final).path).name
    suffix = Path(name).suffix.lower()
    if suffix not in (".xlsx", ".ods"):
        halt(f"{final}: not an .xlsx or .ods file; nothing written")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    tmp = dest / (Path(name).stem + ".download.tmp" + suffix)
    tmp.write_bytes(body)
    try:
        _opens(tmp, suffix)
    except Exception as e:  # noqa: BLE001
        tmp.unlink()
        halt(f"{final}: does not open as {suffix} ({e}); nothing kept")
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


def _file_source(path: Path) -> str:
    """Where a --file was read: the path under the repo (data/raw/...),
    else the absolute path."""
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


def _boundaries(cur) -> set:
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    return {r[0] for r in cur.fetchall()}


def column_exists(cur, table, column) -> bool:
    cur.execute("SELECT 1 FROM information_schema.columns WHERE "
                "table_schema = 'public' AND table_name = %s AND "
                "column_name = %s", (table, column))
    return cur.fetchone() is not None


def _data_names(spec) -> tuple:
    return tuple(c for c in spec.data_cols if c != spec.period_col)


def records(cur, spec, period, edition=None) -> list:
    """The period's rows as records (the spec's data columns, the period as
    a string): the live table's (edition None; null_reasons NULL before ddl
    added it) or the given edition's."""
    names = _data_names(spec)
    if edition is None:
        have = column_exists(cur, spec.live_table, "null_reasons")
        sel = [c if (c != "null_reasons" or have) else "NULL::text"
               for c in names]
        cur.execute(f"SELECT {', '.join(sel)} FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (period,))
    else:
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (period, edition))
    out = [dict(zip(names, r)) for r in cur.fetchall()]
    for r in out:
        r[spec.period_col] = str(period)
    return out


def tip_info(cur, spec) -> dict:
    """{period (int): {edition, source_file, rank}} of every period with
    editions."""
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table}")
    out = {}
    for (y,) in cur.fetchall():
        tip = core.latest_edition(cur, spec, y)
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (y, tip))
        src = {r[0] for r in cur.fetchall()}
        sf = src.pop() if len(src) == 1 else None
        out[int(y)] = {"edition": tip, "source_file": sf,
                       "rank": release_rank(sf)}
    return out


def legacy_tips(cur, part) -> dict:
    """Before the editions tables exist: {held live period: {rank}} (CTB:
    from the live source_publication; 615: LEGACY_615_RANK)."""
    spec = profiles(part)[0].spec
    if part == "ctb":
        cur.execute(f"SELECT {spec.period_col}, array_agg(DISTINCT "
                    f"source_publication) FROM public.{spec.live_table} "
                    "GROUP BY 1")
        return {int(y): {"edition": None, "source_file": None,
                         "rank": max((r for r in map(release_rank, s or ())
                                      if r is not None), default=None)}
                for y, s in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    return {int(y): {"edition": None, "source_file": None,
                     "rank": LEGACY_615_RANK} for (y,) in cur.fetchall()}


def ledger_checks(cur, profile) -> dict:
    """{period (int): {(source_file, file_sha256)}} of the ledger."""
    pc = profile.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(profile)}")
    out = {}
    for y, f, s in cur.fetchall():
        out.setdefault(int(y), set()).add((f, s))
    return out


def _ready(cur, part) -> dict:
    out = {}
    for prof in profiles(part):
        s = prof.spec
        out[s.editions_table] = table_exists(cur, s.editions_table)
        if prof.file_checks:
            t = pe.file_checks_table(prof)
            out[t] = table_exists(cur, t)
        out[f"{s.live_table}.null_reasons"] = column_exists(
            cur, s.live_table, "null_reasons")
    return out


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT,
    source '22'). Called only on committed load, load-615, migrate-legacy
    and restore-edition runs, partial runs included."""
    pe.log_run(cur, PROFILE_CTB, rows_written, notes, started_at)


def _all_profiles() -> tuple:
    return profiles("ctb") + profiles("615")


def cmd_ddl(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for prof in _all_profiles():
                s = prof.spec
                names = [s.editions_table] + (
                    [pe.file_checks_table(prof)] if prof.file_checks else [])
                for t in names:
                    print(f"{t}: {'exists' if table_exists(cur, t) else 'does not exist'}")
                print(f"{s.live_table}.null_reasons: "
                      + ("exists" if column_exists(cur, s.live_table,
                                                   "null_reasons")
                         else "does not exist"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur, tuple(p.spec for p in _all_profiles()))
        pe.finish(conn, args, "ddl: three editions tables and their triggers, "
                              "two file-check ledgers and null_reasons on the "
                              "three live tables present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status_lines(cur, prof) -> list:
    """Per period: the tip's edition and file date; for the CTB main table
    whether live source_publication is uniform and the tip's; whether live
    null_reasons exists and is populated as the tip."""
    s = prof.spec
    lines = []
    have_nr = column_exists(cur, s.live_table, "null_reasons")
    main = s.as_loaded_source_col == "source_publication"
    for y, t in sorted(tip_info(cur, s).items()):
        line = f"  {y}: tip edition {t['edition']} (file dated {t['rank'] or '-'})"
        if main:
            cur.execute(f"SELECT COUNT(DISTINCT source_publication), "
                        f"MIN(source_publication) FROM public.{s.live_table} "
                        f"WHERE {s.period_col} = %s", (y,))
            n, src = cur.fetchone()
            line += ("; live source_publication "
                     + ("uniform, the tip's" if n == 1 and src == t["source_file"]
                        else f"{n} value(s), not the tip's"))
        if not have_nr:
            line += "; null_reasons column absent (run ddl)"
        else:
            cur.execute(f"SELECT COUNT(null_reasons) FROM public.{s.live_table}"
                        f" WHERE {s.period_col} = %s", (y,))
            n_live = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(null_reasons) FROM "
                        f"public.{s.editions_table} WHERE {s.period_col} = %s "
                        "AND edition = %s", (y, t["edition"]))
            n_tip = cur.fetchone()[0]
            line += (f"; null_reasons as the tip ({n_live} rows with reasons)"
                     if n_live == n_tip else
                     f"; null_reasons {n_live} rows, the tip {n_tip} (NULL "
                     "until refresh-latest)")
        lines.append(line)
    return lines


def cmd_status(args) -> int:
    """status: what needs action; exit 1 if anything, or (with a clean
    message, nothing created) if an editions table does not exist."""
    conn = _conn(False)
    try:
        with conn.cursor() as cur:
            missing = [p.spec.editions_table for p in _all_profiles()
                       if not table_exists(cur, p.spec.editions_table)]
            if missing:
                print(f"status: {', '.join(missing)} not present yet; run "
                      "`ddl --commit`, then `migrate-legacy CTB_FILE "
                      "T615_FILE --commit` (S22 has no sync-new)")
                return 1
            ok, texts = True, []
            for prof in _all_profiles():
                st = pe.status(cur, prof)
                ok = ok and st["ok"]
                texts.append(pe.format_status(prof, st,
                                              status_lines(cur, prof)))
            m, c = profiles("ctb")
            tm, tc = tip_info(cur, m.spec), tip_info(cur, c.spec)
            off = sorted(y for y in set(tm) | set(tc)
                         if (tm.get(y) or {}).get("edition")
                         != (tc.get(y) or {}).get("edition"))
            if off:
                ok = False
                texts.append("  CTB main and classes tips differ for "
                             f"{off}: ACTION NEEDED")
    finally:
        conn.rollback()
        conn.close()
    print("\n".join(texts))
    return 0 if ok else 1


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--part ctb|615] [--commit | --simulate]: copy each
    period's latest edition into live, per spec, under the engine's
    before/after guard; for ctb the main and classes plans must name the
    same years, and everything is applied in one transaction."""
    parts = [args.part] if args.part else list(PARTS)
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans = []
            for part in parts:
                profs = profiles(part)
                for prof in profs:
                    if not table_exists(cur, prof.spec.editions_table):
                        halt(f"{prof.spec.editions_table} does not exist; run "
                             "`ddl --commit` and migrate-legacy first")
                counts = [core.refresh_counts(cur, p.spec) for p in profs]
                for prof, cn in zip(profs, counts):
                    print(f"{prof.spec.live_table}: rows refresh-latest would "
                          "write: " + (", ".join(f"{pe._p(k)}={n}"
                                                 for k, n in cn.items())
                                       or "none")
                          + f" (total {sum(cn.values())})")
                if part == "ctb":
                    off = []
                    for y in sorted(set(counts[0]) | set(counts[1])):
                        tips = [core.latest_edition(cur, p.spec, y)
                                for p in profs]
                        if tips[0] != tips[1]:
                            off.append(f"{pe._p(y)} (main edition {tips[0]}, "
                                       f"classes edition {tips[1]})")
                    if off:
                        halt("the CTB main and classes plans differ: their "
                             "latest editions are not the same edition for "
                             + ", ".join(off) + "; they must move together; "
                             "nothing written")
                plans += list(profs)
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            done = []
            for prof in plans:
                pe._refresh_guard(cur, prof.spec, ())
                res = core.refresh_latest(cur, prof.spec)
                if res["updated"]:
                    bad = load_checks.check_latest_equals_live(
                        cur, prof.spec, sorted(res["updated"]))
                    if bad:
                        halt("refresh-latest: live differs from the latest "
                             "edition after the refresh, rolled back: "
                             + "; ".join(bad[:6]))
                done.append(f"{prof.spec.live_table} {res['rows']} rows in "
                            f"{sorted(pe._p(x) for x in res['updated'])}")
        pe.finish(conn, args, "refreshed: " + "; ".join(done)
                  + "; before/after guard passed")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load and load-615
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


def _source(args, part) -> dict:
    """The file the run reads: {path, where, how, page_year}. --file reads a
    local file (nothing downloaded); otherwise the content API finds the
    release (CTB: the newest, or --release YYYY) or the live tables page
    (615) and the file is downloaded to RAW_DIR (also in a preview)."""
    out = {"path": None, "where": None, "how": None, "page_year": None}
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        out.update(path=path, where=_file_source(path), how="local file")
        print(f"file given: {path}; nothing downloaded; identity is read "
              "from the file")
        return out
    if part == "ctb":
        coll = fetch_json(COLLECTION_PATH)
        try:
            if getattr(args, "release", None):
                year, base = release_for_year(coll, args.release)
                how = f"release {year} given"
            else:
                year, base = latest_release(coll)
                how = "newest release (content API)"
        except ValueError as e:
            halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
        page = fetch_json(base)
        py = page_year(page)
        if py != year:
            halt(f"{GOV_UK}{base}: the page title {page.get('title')!r} is "
                 f"not the {year} release")
        try:
            att = la_attachment(page)
        except ValueError as e:
            halt(f"{GOV_UK}{base}: {e}")
        if att["year"] != year:
            halt(f"the attachment {att['title']!r} is for {att['year']}, the "
                 f"release is {year}")
        print(f"release: {page.get('title')} ({GOV_UK}{base}); first "
              f"published {str(page.get('first_published_at'))[:10]}; "
              f"updated {str(page.get('public_updated_at'))[:10]}")
        print(f"  attachment: {att['title']!r}")
        out["page_year"] = year
    else:
        page = fetch_json(LIVE_TABLES_PATH)
        try:
            att = table_615_attachment(page)
        except ValueError as e:
            halt(f"{GOV_UK}{LIVE_TABLES_PATH}: {e}")
        how = "live tables page (content API)"
        print(f"page: {page.get('title')} ({GOV_UK}{LIVE_TABLES_PATH}); "
              f"updated {str(page.get('public_updated_at'))[:10]}")
        print(f"  attachment: {att['title']!r}")
    for ts, note in change_history(page)[:4]:
        print(f"  change_history {ts}: {note[:200]}")
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    path, final = fetch(_abs_url(att["url"]), RAW_DIR)
    print(f"  final URL: {final}")
    out.update(path=path, where=final, how=how)
    return out


def _label(part, file, src, sha, how, *, acknowledged="", older="") -> str:
    head = ("MHCLG Council Taxbase" if part == "ctb"
            else "MHCLG Live Table 615")
    return (f"{head}: {file['title']}; file dated {file['rank']}; "
            f"{src['where']} sha256 {sha[:16]}; {how}"
            + (f"; {older}" if older else "")
            + (f"; ACKNOWLEDGED: {acknowledged}" if acknowledged else ""))


def _build(cur, part, file) -> dict:
    """{year: {'main': [...], 'classes': [...]}} (ctb) or {year: [...]}
    (615), codes resolved; halts on a geography problem."""
    try:
        rmap, problems = resolve_codes(cur, codes_with_numbers(file))
    except ValueError as e:
        halt(f"geography: {e}; nothing stored")
    if part == "ctb":
        un = sorted(c for c, (_, s) in rmap.items() if s == "unmapped")
        if un:
            problems.append(f"the CTB workbook carries codes that are not "
                            f"current authorities: {un}")
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    what = "CTB" if part == "ctb" else "Table 615"
    recoded = sorted((c, v[0]) for c, v in rmap.items() if v[0] and v[0] != c)
    if recoded:
        print(f"{what} codes resolved (geography.resolve, la_code_lookup "
              "recode): " + ", ".join(f"{c} -> {r}" for c, r in recoded))
    un = sorted(c for c, (_, s) in rmap.items() if s == "unmapped")
    if un:
        print(f"{what}: {len(un)} published district code(s) abolished, kept "
              "'unmapped' with lad24cd NULL (never mapped to a successor)")
    try:
        if part == "ctb":
            main, cls = build_ctb(file, lambda c: rmap[c][0])
            return {file["year"]: {"main": main, "classes": cls}}
        return build_615(file, lambda c: rmap[c])
    except ValueError as e:
        halt(f"{e}; nothing stored")


def _load(args, part) -> int:
    profs = profiles(part)
    main_prof = profs[0]
    spec = main_prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            ready = _ready(cur, part)
            has_ed = all(ready.values())
            if writing and not has_ed:
                halt("missing " + ", ".join(t for t, ok in ready.items()
                                           if not ok)
                     + "; run `ddl --commit` and `migrate-legacy CTB_FILE "
                     "T615_FILE --commit` first")
            if has_ed:
                for prof in profs:
                    _, new_live, errors = core.latest_map(cur, prof.spec)
                    if errors:
                        halt("invalid edition chain: " + "; ".join(
                            f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                    if new_live:
                        halt(f"{prof.spec.live_table}: live periods with no "
                             f"editions {[pe._p(x) for x in new_live]}; run "
                             "migrate-legacy first")
                tips = tip_info(cur, spec)
                checked = ledger_checks(cur, main_prof)
                stranded = sorted({int(x) for prof in profs
                                   for x in pe.live_missing_periods(cur, prof)})
            else:
                print("NOTE: " + ", ".join(t for t, ok in ready.items()
                                           if not ok)
                      + " not present yet; this preview compares each period "
                      "with the LIVE tables")
                tips, checked, stranded = legacy_tips(cur, part), {}, []
            ranks = tip_ranks(tips, checked)
            held = sorted(ranks)
            print("held periods: " + (f"{held[0]} .. {held[-1]}" if held
                                      else "none"))
            src = _source(args, part)
            path = src["path"]
            sha = content_sha256(path)
            same = sha == LEGACY_FILES[part]["sha256"]
            print(f"{path.name}: sha256 {sha[:16]}"
                  + ("; byte-identical to the held file the old build read"
                     if same else ""))
            if has_ed and args.recheck is None:
                done = ledger_complete(checked, src["where"], sha, ranks,
                                       allow_older=args.allow_older_file,
                                       stranded=stranded)
                if done:
                    print(f"the file's (source, sha256) is already in the "
                          f"ledger for {done[0]}"
                          + (f"-{done[-1]}" if len(done) > 1 else "")
                          + "; nothing parsed (--recheck YEAR reads it "
                          "again)")
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            try:
                file = read_ctb(path) if part == "ctb" else read_615(path)
            except ValueError as e:
                halt(f"{path.name}: {e}; nothing stored")
            file["source"] = src["where"]
            bad = (ctb_identity_problems(file, src["page_year"],
                                         getattr(args, "release", None))
                   if part == "ctb" else [])
            if bad:
                halt("file identity check failed, nothing stored: "
                     + "; ".join(bad))
            years = file["years"]
            print(f"{path.name}: {file['title']!r}; file dated {file['rank']}"
                  + (f" (first published {file['first_published']}"
                     + (f", revised {file['revised']}" if file['revised']
                        else "") + ")" if part == "ctb" else
                     f" (Latest Update {file['latest_update']!r})")
                  + f"; periods {years[0]}"
                  + (f"-{years[-1]}" if len(years) > 1 else ""))
            for sh, code, y, raw, held_v in file.get("non_integer", ()):
                print(f"  declared non-integer: {sh} {code} {y} published "
                      f"{raw}, held {held_v} (rounded half up)")
            problems = reconcile(file)
            if problems:
                halt("reconciliation inside the file failed (the authorities "
                     "do not sum to England), nothing stored: "
                     + "; ".join(problems[:8]))
            print("reconciled: the authorities sum to England in every used "
                  "column" + (" and year" if part == "615" else ""))
            by_year = _build(cur, part, file)
            lsrc = ledger_source(src["where"], file["rank"], years)
            chk = {y: s for y, s in checked.items() if y not in stranded}
            try:
                new, revised, skipped = plan_periods(
                    years, file["rank"], ranks, chk, lsrc, sha,
                    recheck=args.recheck, allow_older=args.allow_older_file)
            except ValueError as e:
                halt(str(e))
            rc = _run_periods(args, conn, cur, part, profs, has_ed, src, file,
                              by_year, new, revised, skipped, tips, ranks,
                              held, sha, lsrc)
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tip_records(cur, spec, y, tips, has_ed) -> list:
    if has_ed:
        return records(cur, spec, str(y), tips[y]["edition"])
    return records(cur, spec, str(y))


def _run_periods(args, conn, cur, part, profs, has_ed, src, file, by_year,
                 new, revised, skipped, tips, ranks, held, sha, lsrc) -> int:
    started = datetime.now(timezone.utc)
    rank = file["rank"]
    ctb = part == "ctb"
    older_note = {}
    for y in revised:
        t = ranks.get(y)
        if t is not None and t > rank:
            older_note[y] = (f"NOTE: --allow-older-file given; {y}'s tip "
                             f"comes from a file dated {t}, this file is "
                             f"dated {rank}")
            print(older_note[y])
    print(f"planned: new {new or 'none'}; compare {revised or 'none'}; "
          f"skipped " + (", ".join(f"{y} ({r})" for y, r in sorted(
              skipped.items())) or "none"))
    periods = sorted(new + revised)
    if not periods:
        if skipped and all(r.startswith("older") for r in skipped.values()):
            halt(f"every period of the file is skipped: the file is dated "
                 f"{rank}, older than the tips of all its periods; storing it "
                 "would record an older file. If this is deliberate, re-run "
                 "with --allow-older-file")
        print("nothing to do")
        return 0
    ack = set(args.acknowledge or ())
    stray = sorted(ack - set(revised))
    if stray:
        halt(f"--acknowledge {stray}: not compared periods of this run "
             f"({revised or 'none'})")
    latest = max(held) if held else None
    bounds = _boundaries(cur) if not ctb else None
    main_spec = profs[0].spec
    drop = () if has_ed else ("null_reasons",)
    problems, acked, infos = {}, {}, {}
    for y in periods:
        data = by_year.get(y)
        if not data:
            problems[y] = ["the file has no stored row for this period"]
            continue
        newm = data["main"] if ctb else data
        newc = data["classes"] if ctb else []
        tipc = []
        if y in revised:
            kind = "revised"
            tipm = _tip_records(cur, main_spec, y, tips, has_ed)
            if ctb:
                ct = tip_info(cur, profs[1].spec) if has_ed else {}
                tipc = (records(cur, profs[1].spec, str(y), ct[y]["edition"])
                        if has_ed else records(cur, profs[1].spec, str(y)))
        else:
            kind = "new"
            tipm = (_tip_records(cur, main_spec, latest, tips, has_ed)
                    if latest is not None and latest < y else [])
        bad = period_problems(newm, tipm, kind=kind, part=part,
                              boundaries=bounds)
        note = ""
        if kind == "revised":
            fl = _flips(newm, tipm) + _flips(newc, tipc)
            if fl:
                msg = (f"{len(fl)} cell(s) go from 0 to NULL or NULL to 0 "
                       "against the tip (rule 1.10), e.g. "
                       + ", ".join(f"{k} {c} {a}->{b}" for k, c, a, b in fl[:3]))
                if y in ack:
                    acked[y] = note = msg
                    print(f"  {y}: ACKNOWLEDGED (--acknowledge): {msg}")
                else:
                    bad.append(msg + f"; read them, then --acknowledge {y}")
            cols_m = [c for c in profs[0].value_cols if c not in drop]
            differs = _differs(newm, tipm, cols_m)
            if ctb and not differs:
                differs = _differs(newc, tipc, [c for c in profs[1].value_cols
                                                if c not in drop])
            if differs and ranks.get(y) == rank:
                bad.append(f"the file is dated {rank}, the same date as the "
                           "file of the held tip, but its content differs: "
                           "two files claim the same date")
        if bad:
            problems[y] = bad
            continue
        how = ("new period" if kind == "new" else "republished period") + \
            f" ({src['how']})"
        infos[y] = PeriodInfo(
            source_publication(file) if ctb else edition_source_615(file),
            _label(part, file, src, sha, how, acknowledged=note,
                   older=older_note.get(y, "").removeprefix("NOTE: ")),
            ((lsrc, sha),))
    for y, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {y}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [y for y in periods if y not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    main_of = (lambda p: by_year[int(p)]["main"]) if ctb else \
        (lambda p: by_year[int(p)])
    if ok and has_ed:
        if ctb:
            cls_prof = profs[1]

            def compare(c, prof, p, recs, against):
                a = pe.compare_period(c, prof, p, recs, against)
                b = pe.compare_period(c, cls_prof, p,
                                      by_year[int(p)]["classes"], against)
                kind = pair_kind(a["kind"], b["kind"], p)
                ex = list(a["examples"])
                if b["changed"] and b["kind"] != "new":
                    ex.append(f"classes {b['changed']} rows changed"
                              + ("".join(f"; {e}" for e in b["examples"][:2])))
                return {**a, "kind": kind, "examples": ex}

            def apply(c, prof, p, recs, *, fetched_on, source_file=None):
                return _late("apply_ctb_year")(
                    c, prof, cls_prof, p, recs, by_year[int(p)]["classes"],
                    fetched_on=fetched_on, info=infos[int(p)])
        else:
            compare = None

            def apply(c, prof, p, recs, *, fetched_on, source_file=None):
                return _late("apply_615_year")(c, prof, p, recs,
                                               fetched_on=fetched_on,
                                               info=infos[int(p)])
        rc = pe.load_periods(cur, profs[0], [str(y) for y in ok], main_of,
                             rank, args.commit, simulate=args.simulate,
                             against="editions", stats=stats,
                             compare=compare, apply=apply)
    elif ok:
        lp = [dataclasses.replace(p, value_cols=tuple(
            c for c in p.value_cols if c != "null_reasons")) for p in profs]
        compare = None
        if ctb:
            def compare(c, prof, p, recs, against):
                a = pe.compare_period(c, prof, p, recs, against)
                b = pe.compare_period(c, lp[1], p,
                                      by_year[int(p)]["classes"], against)
                ex = list(a["examples"])
                if b["changed"] and b["kind"] != "new":
                    ex.append(f"classes {b['changed']} rows changed")
                return {**a, "kind": pair_kind(a["kind"], b["kind"], p),
                        "examples": ex}
        rc = pe.load_periods(cur, lp[0], [str(y) for y in ok], main_of,
                             rank, False, against="live", compare=compare)
        print("  (editions tables not created yet: preview against live "
              "only)")
    if problems:
        print(f"REJECTED {len(problems)} period(s), nothing stored for them: "
              f"{', '.join(str(y) for y in sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        stored = live = 0  # rows written by this run; not a source value
        for p in stats["periods"]:
            k = stats["kinds"][p]
            n = len(main_of(p))
            if ctb:
                n += len(by_year[int(p)]["classes"])
            if k in ("new", "revised"):
                stored += n
            if k in ("new", LIVE_MISSING):
                live += n
        # a partial run that committed anything (an edition, live rows or
        # only ledger rows) is logged too
        if rc == 0 or stats["periods"]:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}; a period that failed "
                       "is not in the counts below; " if rc else "")
            notes = (partial + f"{part} file {src['where']} sha256 {sha[:16]}"
                     f" dated {rank} ({src['how']}): "
                     + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                        or "nothing stored")
                     + (f"; skipped {sorted(skipped)}" if skipped else "")
                     + "".join(f"; {older_note[y]}" for y in sorted(older_note))
                     + "".join(f"; ACKNOWLEDGED {y}: {acked[y]}"
                               for y in sorted(acked))
                     + f". Rows stored: {stored} edition rows, {live} live "
                     "rows.")
            log_run(cur, stored + live, notes, started)
            conn.commit()
            print("pipeline_run_log row written"
                  + (" (partial run: see its notes)" if rc else ""))
        else:
            print("pipeline_run_log: no row written (a period failed or was "
                  "rejected and nothing was committed)")
    return rc


def cmd_load(args) -> int:
    return _load(args, "ctb")


def cmd_load_615(args) -> int:
    return _load(args, "615")


# ---------------------------------------------------------------------------
# One-off: migrate the held tables
# ---------------------------------------------------------------------------

def live_state(cur, spec) -> tuple:
    """(rows, md5, distinct loaded_at) of a live table: every row as jsonb
    without loaded_at and null_reasons, ordered by key and period."""
    order = ", ".join(tuple(spec.key_cols) + (spec.period_col,))
    cur.execute(f"""SELECT COUNT(*), md5(string_agg((to_jsonb(t)
                    - ARRAY['loaded_at', 'null_reasons']::text[])::text,
                    E'\\n' ORDER BY {order})), COUNT(DISTINCT loaded_at)
                    FROM public.{spec.live_table} t""")
    return tuple(cur.fetchone())


def _proof(name, held, built, cols, key) -> tuple:
    """(cells compared, differences) of held rows against rebuilt rows."""
    hk = {key(r): r for r in held}
    bk = {key(r): r for r in built}
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk), key=str)]
    diffs += [("only rebuilt", k) for k in sorted(set(bk) - set(hk), key=str)]
    cells = 0
    for k in sorted(set(hk) & set(bk), key=str):
        for c in cols:
            cells += 1  # not a source value
            if hk[k].get(c) != bk[k].get(c):
                diffs.append((name, k, c, hk[k].get(c), bk[k].get(c)))
    return cells, diffs


def migration_plan(cur, ctb_file, t615_file, *, legacy=None,
                   files=None) -> dict:
    """Read-only: preconditions, the edition 1 records per period and the
    proof. Halts on any failure (nothing is written here). legacy and files
    default to LEGACY_LIVE and LEGACY_FILES."""
    legacy = legacy or LEGACY_LIVE
    files = files or LEGACY_FILES
    m, c = profiles("ctb")
    t = profiles("615")[0]
    states = {}
    for key, prof in (("ctb", m), ("classes", c), ("615", t)):
        n, h, k = live_state(cur, prof.spec)
        if (n, h) != tuple(legacy[key]) or k != 1:
            halt(f"{prof.spec.live_table} is not as surveyed: {n} rows, hash "
                 f"{h}, {k} loaded_at value(s); expected {legacy[key][0]} "
                 f"rows, hash {legacy[key][1]}, one loaded_at; nothing stored")
        states[key] = (n, h)
    paths = {"ctb": Path(ctb_file), "615": Path(t615_file)}
    reads = {}
    for part, p in paths.items():
        sha = content_sha256(p)
        if sha != files[part]["sha256"]:
            halt(f"{p.name}: sha256 {sha[:16]}, expected "
                 f"{files[part]['sha256'][:16]} (the held {part} file); "
                 "nothing stored")
        try:
            f = read_ctb(p) if part == "ctb" else read_615(p)
        except ValueError as e:
            halt(f"{p.name}: {e}; nothing stored")
        f["source"] = files[part]["url"]
        bad = reconcile(f)
        if bad:
            halt(f"{p.name}: reconciliation failed: {'; '.join(bad[:6])}")
        reads[part] = (f, sha)
    ctb_built = _build(cur, "ctb", reads["ctb"][0])
    t615_built = _build(cur, "615", reads["615"][0])
    # proof: the new parser on the held files reproduces every held cell
    proof, diffs = {}, []
    ctb_years = sorted(ctb_built)
    held_m = [r for y in ctb_years for r in records(cur, m.spec, str(y))]
    held_c = [r for y in ctb_years for r in records(cur, c.spec, str(y))]
    live_years = sorted(legacy_tips(cur, "ctb"))
    if live_years != ctb_years:
        halt(f"live taxbase years {live_years}, the file holds {ctb_years}; "
             "nothing stored")
    built_m = [r for y in ctb_years for r in ctb_built[y]["main"]]
    built_c = [r for y in ctb_years for r in ctb_built[y]["classes"]]
    vals_m = [x for x in CTB_DATA if x != "null_reasons"]
    proof["ctb"] = _proof("ctb", held_m, built_m, vals_m,
                          lambda r: (r["lad24cd"], r["taxbase_year"]))
    proof["classes"] = _proof(
        "classes", held_c, built_c, ["dwellings", "exemption_description"],
        lambda r: (r["lad24cd"], r["taxbase_year"], r["exemption_class"]))
    t_years = sorted(legacy_tips(cur, "615"))
    if t_years != sorted(t615_built):
        halt(f"live 615 years {t_years[:3]}..{t_years[-3:]}, the file holds "
             f"{sorted(t615_built)[:3]}..; nothing stored")
    held_t = [r for y in t_years for r in records(cur, t.spec, str(y))]
    built_t = [r for y in t_years for r in t615_built[y]]
    proof["615"] = _proof("615", held_t, built_t,
                          [x for x in T615_DATA if x != "null_reasons"],
                          lambda r: (r["published_la_code"], r["year"]))
    for k, (_, d) in proof.items():
        diffs += d
    if diffs:
        halt(f"proof failed: {len(diffs)} differences between the held rows "
             f"and the held files read again, e.g. {diffs[:5]}; nothing "
             "stored")
    cross = []
    for y in ctb_years:
        if y in t615_built:
            cross += cross_check_615_ctb(t615_built[y], ctb_built[y]["main"], y)
    if cross:
        halt(f"proof failed: 615 all vacants differs from CTB empty_total + "
             f"unoccupied exemptions in {len(cross)} case(s): {cross[:5]}; "
             "nothing stored")
    per = {"ctb": {}, "615": {}}
    fctb, sctb = reads["ctb"]
    held_src = {}
    cur.execute(f"SELECT {m.spec.period_col}, MIN(source_publication), "
                f"MIN((loaded_at AT TIME ZONE 'UTC')::date) FROM "
                f"public.{m.spec.live_table} GROUP BY 1")
    for y, s, d in cur.fetchall():
        held_src[int(y)] = (s, d)
    for y in ctb_years:
        s, d = held_src[y]
        per["ctb"][y] = {
            "main": [r for r in held_m if r["taxbase_year"] == str(y)],
            "classes": [r for r in held_c if r["taxbase_year"] == str(y)],
            "source_file": f"as loaded: {s}; file dated {fctb['rank']}",
            "loaded": d,
            "ledger": ledger_source(files["ctb"]["url"], fctb["rank"],
                                    fctb["years"]), "sha": sctb}
    f615, s615 = reads["615"]
    cur.execute(f"SELECT MIN((loaded_at AT TIME ZONE 'UTC')::date) FROM "
                f"public.{t.spec.live_table}")
    d615 = cur.fetchone()[0]
    for y in t_years:
        per["615"][y] = {
            "rows": [r for r in held_t if r["year"] == str(y)],
            "source_file": (f"as loaded: {t.spec.live_table} from the old "
                            f"build's single run; {f615['title']}; file "
                            f"dated {f615['rank']}"),
            "loaded": d615,
            "ledger": ledger_source(files["615"]["url"], f615["rank"],
                                    f615["years"]), "sha": s615}
    return {"periods": per, "proof": {k: v[0] for k, v in proof.items()},
            "cross": {y: len(ctb_built[y]["main"]) for y in ctb_years},
            "non_integer": f615["non_integer"], "states": states}


def migrate_legacy(cur, ctb_file, t615_file, *, write, legacy=None,
                   files=None) -> dict:
    """One-off. Preconditions: on write the editions tables and ledgers
    exist and are empty (in a preview they may be absent); live as surveyed
    (LEGACY_LIVE rows and hash, one loaded_at each); the two files' sha256
    as LEGACY_FILES. Edition 1 'as loaded' per period from live (CTB main
    and classes together, one pair hash; 615 per year), with the proof
    (migration_plan): the new parser on the held files reproduces every live
    cell of the three tables, and 615 all vacants equals CTB empty_total +
    unoccupied_exemptions_total for every authority. Ledger rows for both
    files (outcome 'unchanged', edition 1). Live untouched. Halts on any
    failure; the caller rolls everything back."""
    m, c = profiles("ctb")
    t = profiles("615")[0]
    profs = (m, c, t)
    ready = {}
    for prof in profs:
        ready[prof.spec.editions_table] = table_exists(
            cur, prof.spec.editions_table)
        if prof.file_checks:
            ft = pe.file_checks_table(prof)
            ready[ft] = table_exists(cur, ft)
    if write:
        missing = [k for k, v in ready.items() if not v]
        if missing or not all(column_exists(cur, p.spec.live_table,
                                            "null_reasons") for p in profs):
            halt(f"missing {missing or 'a null_reasons column'}; run `ddl "
                 "--commit` first")
    for tbl, ok in ready.items():
        if ok:
            cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
            n = cur.fetchone()[0]
            if n:
                halt(f"{tbl} already holds {n} rows; migrate-legacy runs "
                     "once, on empty tables")
    plan = migration_plan(cur, ctb_file, t615_file, legacy=legacy,
                          files=files)
    pr = plan["proof"]
    print("reconciled inside each file: the authorities sum to England in "
          "every used CTB column and in every Table 615 year on both sheets")
    print(f"proof: the held files read again reproduce every held cell: "
          f"{pr['ctb']:,} CTB cells, {pr['classes']:,} class cells, "
          f"{pr['615']:,} Table 615 cells; 0 differences")
    for sh, code, y, raw, held_v in plan["non_integer"]:
        print(f"  declared: {sh} {code} {y} published {raw}, held {held_v} "
              "(rounded half up)")
    for y, n in plan["cross"].items():
        print(f"proof: Table 615 {y} all vacants equals CTB empty_total + "
              f"unoccupied_exemptions_total for {n} of {n} authorities")
    for y, py in sorted(plan["periods"]["ctb"].items()):
        print(f"  CTB {y}: edition 1 as loaded, {len(py['main'])} main rows, "
              f"{len(py['classes'])} class rows; ledger {py['ledger']}")
    t615 = plan["periods"]["615"]
    if t615:
        print(f"  615 {min(t615)}-{max(t615)}: edition 1 as loaded for "
              f"{len(t615)} years, {sum(len(v['rows']) for v in t615.values()):,}"
              " rows (" + ", ".join(f"{y} {len(v['rows'])}"
                                     for y, v in sorted(t615.items())) + ")")
    if not write:
        return plan
    for y, py in sorted(plan["periods"]["ctb"].items()):
        p = str(y)
        cur.execute(f"SAVEPOINT {m.savepoint}")
        sha = pair_sha(py["main"], py["classes"], m.spec, c.spec)
        for prof, recs in ((m, py["main"]), (c, py["classes"])):
            ed = core.insert_edition(
                cur, prof.spec, recs, p, release_label=core.AS_LOADED_LABEL,
                published_date=py["loaded"], source_file=py["source_file"],
                source_sha256=sha, supersedes=None, strict=True)
            if ed != 1 or core.rows_differing(cur, prof.spec, p, ed):
                halt(f"{prof.spec.editions_table} {y}: edition 1 does not "
                     "equal live; rolled back")
        pe.record_file_check(cur, m, p, py["ledger"], py["sha"], "unchanged",
                             1)
        cur.execute(f"RELEASE SAVEPOINT {m.savepoint}")
    for y, py in sorted(plan["periods"]["615"].items()):
        p = str(y)
        cur.execute(f"SAVEPOINT {t.savepoint}")
        ed = core.insert_edition(
            cur, t.spec, py["rows"], p, release_label=core.AS_LOADED_LABEL,
            published_date=py["loaded"], source_file=py["source_file"],
            source_sha256=core.rows_sha256(t.spec, py["rows"]),
            supersedes=None, strict=True)
        if ed != 1 or core.rows_differing(cur, t.spec, p, ed):
            halt(f"{t.spec.editions_table} {y}: edition 1 does not equal "
                 "live; rolled back")
        pe.record_file_check(cur, t, p, py["ledger"], py["sha"], "unchanged",
                             1)
        cur.execute(f"RELEASE SAVEPOINT {t.savepoint}")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, args.ctb_file, args.t615_file,
                                  write=writing)
            if args.commit:
                per = plan["periods"]
                n = (sum(len(v["main"]) + len(v["classes"])
                         for v in per["ctb"].values())
                     + sum(len(v["rows"]) for v in per["615"].values()))
                log_run(cur, n, "migrate-legacy: edition 1 as loaded for CTB "
                        f"{sorted(per['ctb'])} (main and classes) and Table "
                        f"615 {min(per['615'])}-{max(per['615'])}; proof "
                        f"{plan['proof']['ctb']} CTB, "
                        f"{plan['proof']['classes']} class and "
                        f"{plan['proof']['615']} Table 615 cells equal the "
                        "held files read again; 615 all vacants equals CTB "
                        "empty_total + unoccupied exemptions for every "
                        "authority; ledger rows for both files. Live "
                        "untouched.", started)
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
    from edition N') for refresh-latest to apply; return its number. For
    ctb the main and classes editions are restored together (same number).
    Halts if the edition does not exist or is the tip."""
    p = str(period)
    profs = profiles(part)
    recs, tips = [], []
    for prof in profs:
        tips.append(core.latest_edition(cur, prof.spec, p))
        rs = records(cur, prof.spec, p, edition)
        if not rs:
            halt(f"{prof.spec.editions_table} {period}: no edition {edition}")
        recs.append(rs)
    if len(set(tips)) != 1:
        halt(f"{period}: the tips differ {tips}; nothing restored")
    tip = tips[0]
    if edition == tip:
        halt(f"{period}: edition {edition} is the tip; nothing to restore")
    s0 = profs[0].spec
    cur.execute(f"SELECT DISTINCT source_file FROM public.{s0.editions_table}"
                f" WHERE {s0.period_col} = %s AND edition = %s", (p, edition))
    src = cur.fetchone()[0]
    sha = (pair_sha(recs[0], recs[1], profs[0].spec, profs[1].spec)
           if part == "ctb" else core.rows_sha256(s0, recs[0]))
    new = []
    for prof, rs in zip(profs, recs):
        new.append(core.insert_edition(
            cur, prof.spec, rs, p,
            release_label=f"restored from edition {edition}",
            published_date=date.today(), source_file=src,
            source_sha256=sha, supersedes=tip, strict=True,
            allow_revert=True))
    if len(set(new)) != 1 or new[0] == tip:
        halt(f"{period}: edition {edition} was not stored as the next "
             f"edition ({new}); nothing to restore")
    return new[0]


def cmd_restore_edition(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            for prof in profiles(args.part):
                if not table_exists(cur, prof.spec.editions_table):
                    halt(f"{prof.spec.editions_table} does not exist")
            new = restore_edition(cur, args.part, args.period, args.edition)
            print(f"{args.part} {args.period}: edition {args.edition} "
                  + ("stored" if writing else "would be stored")
                  + f" as edition {new}; run refresh-latest to apply it")
            if args.commit:
                n = sum(len(records(cur, p.spec, str(args.period), new))
                        for p in profiles(args.part))
                log_run(cur, n, f"restore-edition: {args.part} {args.period} "
                        f"edition {args.edition} stored as edition {new}",
                        started)
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

def _load_options(p) -> None:
    p.add_argument("--file", metavar="PATH",
                   help="a local file (nothing downloaded; identity is read "
                   "from the file)")
    p.add_argument("--recheck", type=_year_arg, metavar="YEAR",
                   help="read this held period again although the file is "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store periods whose tip comes from a "
                   "newer file (logged)")
    p.add_argument("--acknowledge", action="append", type=_year_arg,
                   metavar="YEAR",
                   help="release a period's 0/NULL changes after reading the "
                   "preview (repeatable)")
    pe.mode_parser(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="S22 MHCLG Council Taxbase and Live Table 615: editions "
        "loader on the period-editions engine. There is no sync-new; "
        "migrate-legacy records the held periods once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the three editions tables, their "
                       "triggers and the two ledgers, and add null_reasons "
                       "to the live tables (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read the Council Taxbase release's local "
                       "authority level workbook, compare and store its year "
                       "(preview by default)")
    p.add_argument("--release", type=_year_arg, metavar="YYYY",
                   help="that year's release page (default the newest)")
    _load_options(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("load-615", help="read Live Table 615, compare and "
                       "store each year (preview by default)")
    _load_options(p)
    p.set_defaults(func=cmd_load_615)
    p = sub.add_parser("refresh-latest", help="copy each period's latest "
                       "edition into the live tables (preview by default)")
    p.add_argument("--part", choices=PARTS,
                   help="ctb (main and classes together) or 615; default both")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for every held period, with the proof (preview by "
                       "default)")
    p.add_argument("ctb_file", metavar="CTB_FILE")
    p.add_argument("t615_file", metavar="T615_FILE")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("part", choices=PARTS)
    p.add_argument("period", type=_year_arg)
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
