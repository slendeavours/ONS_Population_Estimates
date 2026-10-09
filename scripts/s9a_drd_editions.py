"""S9a (NHS England Discharge Ready Date, DRD): edition history and loader on
the period-editions engine.

nhs_drd_discharge_delays holds one row per upper-tier authority (UTLA) and
month: the latest layer. This module owns nhs_drd_discharge_delays_editions,
which keeps every file of a month that differed from the one before, so a
revision (NHS England republishes a year's months as '-Revised' files, once a
year) never overwrites what was held. The machinery is editions_core driven by
SPEC, through period_editions (pe) bound to PROFILE. The pure part (links,
parsing the UTLA Acceptable sheet, records, identity) comes first and keeps the
parsing behaviour of the retired s9a_drd_build.py; the loader follows.
Importing this module needs no database, no network and no key.

Unlike S15 and S18, NHS England publishes ONE FILE PER MONTH. A revision is a
replacement file for that month (a new URL), so each month is fetched on its
own, the month's current link is compared with the file its latest edition
came from, and every file read is recorded in the file-check ledger
(nhs_drd_discharge_delays_editions_file_checks) so an unchanged reissue is not
downloaded again. --recheck-all and --recheck PERIOD re-read held months.

Blanks and zeros (docs/RULES.md rule 1): '-' and a blank cell are NULL, never
0; any other text in a value cell halts; a published 0 stays 0; 0 to NULL or
NULL to 0 against the latest edition needs --acknowledge PERIOD. Counts must be
whole numbers. Floats are stored as Decimal(repr(value)), which is what the
live numeric columns hold.

Identity (rule 3) is read from the file: the Cover Sheet and UTLA Acceptable
sheet 'Period:' must be the link's month, 'Revised:' must be a date exactly
when the link name says -Revised, and the title must be the DRD title.

Barnsley and Sheffield (rule 4): the workbooks publish E08000016/19 (declared
'old' for source 9a in scripts/geography.py); the check and the recode map
come only from geography.resolve. Codes are checked against utla_lad_mapping
(UTLAs include 21 E10 counties, which are not in la_boundaries).

Live `source` stays the file URL (the views and check_sources.py read it); an
edition's source_file is that URL exactly.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back):
    python scripts/s9a_drd_editions.py ddl [--commit | --simulate]
    python scripts/s9a_drd_editions.py status
    python scripts/s9a_drd_editions.py sync-new [--expected-areas N]
                                                [--commit | --simulate]
    python scripts/s9a_drd_editions.py load [--file F] [--min-period D]
                                            [--recheck-all] [--recheck P ...]
                                            [--expected-areas N]
                                            [--allow-older-file]
                                            [--acknowledge P ...]
                                            [--commit | --simulate]
        # new = months on the page not held (later than the earliest held, or
        # from --min-period); recheck = held months whose current link is
        # neither the latest edition's file nor already in the ledger, plus
        # --recheck / --recheck-all, plus editions months missing from live.
        # A month that fails its checks (areas, negative counts, percentages
        # outside 0..1) or breaks a stop condition is REJECTED (not stored,
        # exit 1). Downloads go to data/raw/s9a_drd/ (also in a preview;
        # a preview writes nothing to the database).
    python scripts/s9a_drd_editions.py refresh-latest [--commit | --simulate]
                                                      [--accept-drift PERIOD]
"""
import argparse
import dataclasses
import hashlib
import html as html_lib
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

MIN_PERIOD = "2024-04-01"
LIVE = "nhs_drd_discharge_delays"
TABLE = "nhs_drd_discharge_delays_editions"
RUN_AGENT = "Source 9a - NHS England DRD"
RUN_SOURCE = "9a"
SOURCE_FILE = "NHS England DRD monthly webfile"
LIVE_MISSING = pe.LIVE_MISSING

PAGE = ("https://www.england.nhs.uk/statistics/statistical-work-areas/"
        "discharge-delays/discharge-ready-date/")
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s9a_drd"      # git-ignored
USER_AGENT = ("ucws-pipeline S9a loader (read-only download of the public "
              "NHS England DRD webfiles)")
MIN_FILE_BYTES = 100 * 1024
DRD_TITLE = "Timeliness of Acute Hospital Discharges"
WEBFILE_MARK = "discharge-ready-date-monthly-data-webfile"

MAX_NULL_AREAS = 5            # stop: NULL replacing a number, areas per month
MAX_BIG_REVISION_AREAS = 10   # stop: bed days revised above 50%, areas
PRECISION = Decimal("1e-8")   # below this a change is precision only

# (record field, 0-based column in the UTLA Acceptable sheet, kind, expected
# header text). kind: id, name, count (whole number) or ratio (any number).
COLUMNS = (
    ("utla_code", 1, "id", "UTLA Code"),
    ("utla_name", 2, "name", "UTLA"),
    ("pct_acceptable_trust_coverage", 8, "ratio",
     "% of all UTLA discharges that are from acceptable trusts"),
    ("total_discharges", 9, "count",
     "Total Discharges of UTLA patients from provider"),
    ("total_discharges_acceptable_trusts", 10, "count",
     "Total Discharges for UTLA from acceptable trusts"),
    ("total_bed_days_lost", 11, "count",
     "Total bed days lost due to delayed discharge"),
    ("pct_same_day_discharge", 13, "ratio",
     "Date of discharge is same as Discharge Ready Date"),
    ("pct_delayed_1plus_days", 14, "ratio",
     "Date of Discharge is 1+ days after Discharge Ready Date"),
    ("discharged_no_delay", 16, "count", "No delay"),
    ("discharged_1_day", 17, "count", "1 day delay"),
    ("discharged_2_3_days", 18, "count", "2-3 day delay"),
    ("discharged_4_6_days", 19, "count", "4-6 day delay"),
    ("discharged_7_13_days", 20, "count", "7-13 day delay"),
    ("discharged_14_20_days", 21, "count", "14-20 day delay"),
    ("discharged_21_plus_days", 22, "count", "21 days or more"),
    ("avg_days_drd_to_discharge_inc_zero", 46, "ratio",
     "Average days from Discharge Ready Date to date of discharge "
     "(inc 0 day delays)"),
    ("avg_days_drd_to_discharge_exc_zero", 47, "ratio",
     "Average days from Discharge Ready Date to date of discharge "
     "(exc 0 day delays)"),
)
EXPECTED_HEADERS = {pos: text for _, pos, _, text in COLUMNS}
VALUE_COLUMNS = tuple(n for n, _, k, _ in COLUMNS if k in ("count", "ratio"))
COUNT_COLUMNS = tuple(n for n, _, k, _ in COLUMNS if k == "count")
RATIO_COLUMNS = tuple(n for n, _, k, _ in COLUMNS if k == "ratio")
PCT_COLUMNS = ("pct_acceptable_trust_coverage", "pct_same_day_discharge",
               "pct_delayed_1plus_days")
BIG_COLUMN = "total_bed_days_lost"
NUMERIC_COLUMNS = RATIO_COLUMNS   # precision-only applies to these
UTLA_CODE_RE = re.compile(r"E(06|08|09|10)[0-9]{6}")
DROPPED_CODES = ("#N/A", "NULL")   # non-English rows in the aggregate block
MARKER = "-"

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_NAMES = {}
for _i, _m in enumerate(_MONTHS, 1):
    _MONTH_NAMES[_m] = _i
    _MONTH_NAMES[_m[:3]] = _i
_MONTH_NAMES["sept"] = 9
_PERIOD_RE = re.compile(
    r"(?<![a-z])(" + "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
    + r")[-_ ]?([0-9]{4})(?![0-9])")
_UPLOAD_RE = re.compile(r"/uploads/sites/[0-9]+/([0-9]{4})/([0-9]{2})/")
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_DATE_RE = re.compile(r"([0-9]{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+"
                      r"([0-9]{4})")


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def period_from_link(url) -> "str | None":
    """'YYYY-MM-01' named by a DRD file link or name (full and short month
    names, 'Sept', with or without a separator: 'March-2024-revised',
    'November2023'); None if the name carries none."""
    m = _PERIOD_RE.search(_name(url if isinstance(url, str) else "").lower())
    if not m:
        return None
    return f"{int(m.group(2)):04d}-{_MONTH_NAMES[m.group(1)]:02d}-01"


def _is_revised(url: str) -> bool:
    return "revised" in _name(url).lower()


def link_rank(url: str) -> tuple:
    """(upload year, upload month, 1 for a -Revised link, else zero): the upload path
    YYYY/MM first, then -Revised. A link with no upload path ranks (0, 0, r)."""
    m = _UPLOAD_RE.search(url or "")
    y, mo = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return (y, mo, 1 if _is_revised(url or "") else 0)  # not a source value


def link_older(chosen: str, tip_file: "str | None") -> bool:
    """True if the chosen link ranks below the file a month's latest edition
    came from. Where either has no upload path only -Revised is compared (an
    original after a -Revised is older)."""
    if not tip_file:
        return False
    a, b = link_rank(chosen), link_rank(tip_file)
    if a[0] and b[0]:
        return a < b
    return a[2] < b[2]


def find_month_links(html: str) -> dict:
    """{period: [url, ...]} of the monthly webfile xlsx links on the page (the
    timeseries workbook, CSV files and anything else are left out; periods
    come from the file name; each url once, in page order)."""
    out = {}
    for href in re.findall(r"""href=["']([^"']+)["']""", html or "", re.I):
        url = html_lib.unescape(href)
        path = urlsplit(url).path.lower()
        if not path.endswith(".xlsx") or WEBFILE_MARK not in path:
            continue
        if "timeseries" in path or "time-series" in path:
            continue
        period = period_from_link(url)
        if period is None:
            continue
        url = urljoin(PAGE, url)
        if url not in out.setdefault(period, []):
            out[period].append(url)
    return out


def choose_link(urls) -> str:
    """The link of highest link_rank; two that tie raise ValueError naming
    both (no winner is guessed)."""
    urls = list(dict.fromkeys(urls))
    if not urls:
        raise ValueError("no link to choose from")
    best = max(link_rank(u) for u in urls)
    top = [u for u in urls if link_rank(u) == best]
    if len(top) > 1:
        raise ValueError("two links for one month rank the same "
                         f"{best}: {top[0]} and {top[1]}; refusing to choose")
    return top[0]


def csv_reissue_notes(html: str, chosen: dict) -> list:
    """Notes for CSV files reissued with a version suffix ('...-April-2026v2.
    csv'): the loader reads the webfile only. chosen: {period: webfile link}."""
    notes = []
    for href in re.findall(r"""href=["']([^"']+)["']""", html or "", re.I):
        url = html_lib.unescape(href)
        name = _name(url)
        if not name.lower().endswith(".csv") or "discharge-ready" not in \
                name.lower():
            continue
        if not re.search(r"v[0-9]+\.csv$", name.lower()):
            continue
        period = period_from_link(url)
        web = chosen.get(period)
        notes.append(
            f"NOTE: CSV {name} is a reissue (version suffix) for "
            f"{period or 'an unknown month'}; the loader reads the webfile "
            "only"
            + (f" ({_name(web)} is held as the month's file)" if web
               else " (no webfile for that month on the page)"))
    return sorted(set(notes))


# ---------------------------------------------------------------------------
# Reading a workbook
# ---------------------------------------------------------------------------

def _norm(v) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def _parse_date(v):
    """A date cell: datetime/date, or text like '13th  August 2026'; None for
    '-' or blank; ValueError for anything else."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = _norm(v)
    if s in ("", MARKER):
        return None
    m = _DATE_RE.fullmatch(s)
    if m and m.group(2).lower() in _MONTH_NAMES:
        return date(int(m.group(3)), _MONTH_NAMES[m.group(2).lower()],
                    int(m.group(1)))
    raise ValueError(f"not a date: {v!r}")


def _labels(ws, limit=30) -> dict:
    """{label without colon (lower): value} of the 'Label:' / value pairs in
    the first rows of a sheet."""
    out = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i >= limit:
            break
        cells = list(row)
        for j, c in enumerate(cells[:-1]):
            if isinstance(c, str) and c.strip().endswith(":"):
                key = c.strip()[:-1].strip().lower()
                if key not in out:
                    nxt = next((x for x in cells[j + 1:] if x is not None),
                               None)
                    out[key] = nxt
                break
    return out


def _utla_sheet(wb, name):
    hits = [s for s in wb.sheetnames
            if re.fullmatch(r"utla\s*acceptable", s.strip(), re.I)]
    if len(hits) != 1:
        raise ValueError(f"{name}: expected one sheet 'UTLA Acceptable', "
                         f"found {hits} (sheets {wb.sheetnames})")
    return wb[hits[0]]


def read_cover(path) -> dict:
    """The file's own identity: {'period', 'published', 'revised', 'title'}
    from the Cover Sheet (dates as date, or None; revised None for '-'), plus
    'utla_period' and 'utla_revised' from the UTLA Acceptable sheet's own
    header. ValueError if a sheet or a date cannot be read."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        name = Path(path).name
        if "Cover Sheet" not in wb.sheetnames:
            raise ValueError(f"{name}: no 'Cover Sheet' (sheets "
                             f"{wb.sheetnames})")
        cov = _labels(wb["Cover Sheet"])
        utla = _labels(_utla_sheet(wb, name))
    finally:
        wb.close()

    def d(src, key):
        try:
            return _parse_date(src.get(key))
        except ValueError as e:
            raise ValueError(f"{name}: '{key.title()}:' {e}")
    return {"period": d(cov, "period"), "published": d(cov, "published"),
            "revised": d(cov, "revised"),
            "title": _norm(cov.get("title")) or None,
            "utla_period": d(utla, "period"),
            "utla_revised": d(utla, "revised")}


def check_identity(link_period: str, cover: dict, link_name: str) -> list:
    """Problems (empty = fine): the Cover Sheet and UTLA sheet 'Period:' must
    be the link's month; 'Revised:' must be a date exactly when the name says
    -Revised (on both sheets); the title must be the DRD title."""
    problems = []
    want = date.fromisoformat(link_period)
    for label, got in (("Cover Sheet", cover.get("period")),
                       ("UTLA Acceptable sheet", cover.get("utla_period",
                                                           cover.get("period")))):
        if got != want:
            problems.append(f"{label} Period: is {got}, the link {link_name} "
                            f"names {link_period}")
    named = "revised" in (link_name or "").lower()
    for label, rev in (("Cover Sheet", cover.get("revised")),
                       ("UTLA Acceptable sheet", cover.get("utla_revised",
                                                           cover.get("revised")))):
        if named and rev is None:
            problems.append(f"{link_name} says -Revised but the {label} has "
                            "no Revised: date")
        elif not named and rev is not None:
            problems.append(f"the {label} has Revised: {rev} but {link_name} "
                            "is not named -Revised")
    if DRD_TITLE.lower() not in (cover.get("title") or "").lower():
        problems.append(f"the title is {cover.get('title')!r}, not the DRD "
                        f"title ({DRD_TITLE}...)")
    return problems


def _value(v, kind, where):
    """A value cell -> int / Decimal / None. '-' and blank are NULL; any
    other text, a non-whole count or a non-finite number raises."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"{where}: boolean {v!r} in a value cell")
    if isinstance(v, str):
        if v.strip() in (MARKER, ""):
            return None
        raise ValueError(f"{where}: unknown marker or text {v!r} in a value "
                         "cell (only '-' is a known marker)")
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        if f != f or f in (float("inf"), float("-inf")):
            raise ValueError(f"{where}: non-finite number {v!r}")
        if kind == "count":
            if f != int(f):
                raise ValueError(f"{where}: count {v!r} is not a whole number")
            return int(f)
        return Decimal(repr(v)) if isinstance(v, float) else Decimal(v)
    raise ValueError(f"{where}: unreadable value {v!r}")


def parse_workbook(path) -> tuple:
    """(rows, dropped): the 'UTLA Aggregate' rows of the UTLA Acceptable sheet
    as dicts {utla_code, utla_name, <15 value columns>} with the publisher's
    own codes (E06/E08/E09/E10; not recoded), and {marker: count} of the
    dropped non-English rows ('#N/A', 'NULL'). The header row is found by
    'Summary Type' / 'UTLA Code' and checked against EXPECTED_HEADERS at the
    17 column positions (any difference raises: no silent column shift).
    '-' is None; any other text in a value cell raises; floats are
    Decimal(repr(v)). Any other code raises."""
    import openpyxl
    name = Path(path).name
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        it = _utla_sheet(wb, name).iter_rows(values_only=True)
        header = None
        for i, r in enumerate(it):
            if i > 40:
                break
            if (r and _norm(r[0]) == "Summary Type" and len(r) > 1
                    and _norm(r[1]) == "UTLA Code"):
                header = r
                break
        if header is None:
            raise ValueError(f"{name}: no header row with 'Summary Type' and "
                             "'UTLA Code' in the first rows of UTLA "
                             "Acceptable")
        for pos, text in EXPECTED_HEADERS.items():
            got = _norm(header[pos]) if pos < len(header) else ""
            if got != text:
                raise ValueError(f"{name}: header changed at column {pos}: "
                                 f"{got!r}, expected {text!r}; columns may "
                                 "have shifted, nothing read")
        need = max(EXPECTED_HEADERS) + 1
        rows, dropped = [], {}
        for r in it:
            if not r or len(r) < need or _norm(r[0]) != "UTLA Aggregate":
                continue
            code = _norm(r[1])
            if code in DROPPED_CODES:
                dropped[code] = dropped.get(code, 0) + 1
                continue
            if not UTLA_CODE_RE.fullmatch(code):
                raise ValueError(f"{name}: UTLA Aggregate row with code "
                                 f"{code!r}, expected E06/E08/E09/E10 (or "
                                 "'#N/A' / 'NULL', dropped)")
            rec = {"utla_code": code, "utla_name": _norm(r[2]) or None}
            for field, pos, kind, _ in COLUMNS:
                if kind in ("count", "ratio"):
                    rec[field] = _value(r[pos], kind, f"{name} {code} {field}")
            rows.append(rec)
    finally:
        wb.close()
    return rows, dropped


def build_records(rows, recodes, period) -> list:
    """Rows -> records for one month: utla_code through recodes (publisher
    code -> canonical; any other unchanged), reporting_period = period,
    sorted by code. ValueError on a duplicate code (two source codes meeting
    or a repeated row): no winner is chosen."""
    out, seen = [], {}
    for r in rows:
        code = recodes.get(r["utla_code"], r["utla_code"])
        if code in seen:
            raise ValueError(f"duplicate record for utla_code {code}, period "
                             f"{period}: source codes {seen[code]} and "
                             f"{r['utla_code']}; refusing to choose a winner")
        seen[code] = r["utla_code"]
        out.append({**r, "utla_code": code, "reporting_period": period})
    out.sort(key=lambda x: x["utla_code"])
    return out


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def precision_only(old, new) -> bool:
    """True if two non-NULL values differ by less than 1e-8: a rounding
    difference (the June 2026 CSV was rounded to about 9 decimal places), not
    a publisher revision. NULL on either side is never precision only."""
    if old is None or new is None:
        return False
    return abs(Decimal(old) - Decimal(new)) < PRECISION


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

# Live table (checked 2026-10-09 in information_schema): reporting_period date
# and utla_code varchar (NOT NULL, primary key together), utla_name varchar,
# total_discharges, total_discharges_acceptable_trusts, total_bed_days_lost and
# the seven discharged_* columns integer, the five percentage/average columns
# numeric (no scale), source text, loaded_at timestamptz default now(). The
# editions table matches it on key and values; no lad24cd foreign key (the 21
# E10 counties are not in la_boundaries).
_TYPES = {k: ("integer" if k == "count" else "numeric")
          for k in ("count", "ratio")}
SPEC = core.EditionSpec(
    name="s9a",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("utla_code",),
    period_col="reporting_period",
    value_cols=tuple((n, _TYPES[k]) for n, _, k, _ in COLUMNS
                     if k in _TYPES),
    extra_cols=(("utla_name", "varchar"),),
    refresh_cols=VALUE_COLUMNS + ("utla_name", "source"),
    refresh_from=(("source", "source_file"),),
    as_loaded_source_col="source",
    refresh_source_whole_period=True,   # one source per refreshed month
    key_types=(("reporting_period", "date NOT NULL"),
               ("utla_code", "varchar NOT NULL")),
    fk_la_boundaries=False,
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S9a release label comes from the file; store "
                     "through apply_period(...)")


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s9a_drd_editions, name, ...) reaches the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUE_COLUMNS, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S9a NHS England DRD",
    default_source_file=SOURCE_FILE,
    expected_areas=None,   # derived at run time from the held modal count
    release_label=_no_label, file_checks=True,
    savepoint="s9a_period",
    example_label="(DRD values)")


def _profile(spec) -> pe.Profile:
    return PROFILE.with_spec(spec)


def check_period(records: list, period: str, expected_areas,
                 required_areas) -> None:
    """ValueError unless the month is whole: an ISO date, every record of it,
    no code repeated, no negative count, every percentage within 0..1,
    exactly expected_areas areas (None: not checked) and every area of
    required_areas (the live table's latest month)."""
    if not isinstance(period, str) or not _ISO_DAY.fullmatch(period):
        raise ValueError(f"not an ISO date period: {period!r}")
    seen = set()
    for r in records:
        if r.get("reporting_period") != period:
            raise ValueError(f"{period}: a record of period "
                             f"{r.get('reporting_period')!r}")
        c = r["utla_code"]
        if c in seen:
            raise ValueError(f"{period}: {c} appears twice")
        seen.add(c)
        for col in COUNT_COLUMNS:
            if r[col] is not None and r[col] < 0:
                raise ValueError(f"{period}: {c} {col} is negative "
                                 f"({r[col]})")
        for col in PCT_COLUMNS:
            if r[col] is not None and not 0 <= r[col] <= 1:
                raise ValueError(f"{period}: {c} {col} is outside 0..1 "
                                 f"({r[col]})")
    n = len(seen)
    if expected_areas is not None and n != expected_areas:
        why = ("a short month is never stored" if n < expected_areas else
               "more areas than held; review them, then give "
               "--expected-areas N")
        raise ValueError(f"{period}: {n} areas, expected {expected_areas}; "
                         f"{why}")
    missing = sorted(set(required_areas) - seen)
    if missing:
        raise ValueError(f"{period}: {len(missing)} area(s) of the live "
                         f"table's latest month missing {missing[:10]}; a "
                         "month missing an area is never stored")


def period_problems(by_period: dict, periods, expected_areas,
                    required_areas) -> dict:
    """{period: message} for each month that fails check_period."""
    out = {}
    for p in periods:
        try:
            check_period(by_period[p], p, expected_areas, required_areas)
        except ValueError as e:
            out[p] = str(e)
    return out


def run_profile(spec, expected_areas, required_areas=()) -> pe.Profile:
    """PROFILE for one run: the spec, the expected area count and the
    per-month check."""
    req = frozenset(required_areas)
    return dataclasses.replace(
        _profile(spec), expected_areas=expected_areas,
        check_records=lambda r, p: check_period(r, p, expected_areas, req))


def insert_live(cur, profile, period: str, records: list) -> None:
    """The month's live rows with utla_name and `source` (the file URL, taken
    from each record's 'source' key); loaded_at takes its default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = (tuple(spec.key_cols) + (spec.period_col,)
            + tuple(profile.value_cols) + ("utla_name", "source"))
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r[c] for c in profile.value_cols)
         + (r.get("utla_name"), r.get("source")) for r in records],
        page_size=1000)


@dataclass(frozen=True)
class FileInfo:
    """One month's file: its link (the edition's source_file), the path read,
    the file sha256, the release label and the file's own dates."""
    url: str
    path: Path
    sha256: str
    label: str


def release_label(kind: str, cover: dict, sha: str,
                  acknowledged: str = "") -> str:
    rev = cover.get("revised")
    return (f"NHS England DRD webfile {cover['period']:%Y-%m}, {kind}; "
            f"published {cover.get('published') or '-'}; revised "
            f"{rev or '-'}; file sha256 {sha[:16]}"
            + (f"; ACKNOWLEDGED: {acknowledged}" if acknowledged else ""))


def apply_period(cur, profile, period, records, *, fetched_on, info):
    """pe.apply_period for one month's file, then the file-check ledger row
    (in the month's savepoint, so it commits or rolls back with the month).
    Edition and live rows are stored by the engine."""
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
                           source_file=info.url,
                           insert=_late("insert_live"))
    pe.record_file_check(cur, prof, period, info.url, info.sha256, kind,
                         core.chain_tip(cur, prof.spec, period))
    return kind


# ---------------------------------------------------------------------------
# Files: page and download
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


def fetch_page(session=None) -> str:
    """The publication page's HTML (a read-only GET)."""
    return _get(_session(session), PAGE).text


def fetch_month(url, dest=None, session=None) -> Path:
    """Download one month's webfile to dest (default data/raw/s9a_drd/) under
    the link's file name and return the path to read. Checked before it is
    kept: more than MIN_FILE_BYTES and opening with a 'UTLA Acceptable'
    sheet. A same-named file with the same content is kept as it is; one with
    different content is never replaced: the new download is saved beside it
    as <stem>-<sha8><ext> and that is the one read."""
    dest = Path(dest) if dest is not None else RAW_DIR
    body = _get(_session(session), url).content
    if len(body) <= MIN_FILE_BYTES:
        halt(f"{url}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written; saw {body[:100]!r}")
    dest.mkdir(parents=True, exist_ok=True)
    name = _name(url)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    tmp = dest / (Path(name).stem + ".download.tmp.xlsx")
    tmp.write_bytes(body)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(tmp, read_only=True)
        try:
            _utla_sheet(wb, name)
        finally:
            wb.close()
    except Exception as e:  # noqa: BLE001
        tmp.unlink()
        halt(f"{url}: not a usable DRD webfile ({e}); nothing kept")
    path = target
    if target.exists():
        old_sha = content_sha256(target)
        if old_sha == new_sha:
            tmp.unlink()
            print(f"{name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
        else:
            path = dest / f"{target.stem}-{new_sha[:8]}{target.suffix}"
            tmp.replace(path)
            print(f"NOTE: {name} already exists with different content "
                  f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
                  f"untouched and saved the new download as {path.name}")
    else:
        tmp.replace(target)
        print(f"downloaded {name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    return path


def valid_codes(cur) -> set:
    """The UTLA codes of utla_lad_mapping (E10 counties included)."""
    cur.execute("SELECT DISTINCT utla_code FROM public.utla_lad_mapping")
    return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Window and cell summary
# ---------------------------------------------------------------------------

def plan_months(held, page, tips, checked, *, recheck_all=False, recheck=(),
                min_period=None) -> tuple:
    """(new, recheck, earlier), each ascending. page: {period: chosen link};
    tips: {period: file the month's latest edition came from}; checked:
    {period: {links already read}}. new: on the page, not held, from
    min_period or (without it) later than the earliest held; earlier: on the
    page, not held, before that (never loaded). recheck: held months on the
    page whose chosen link is neither the tip's file nor in checked, plus
    every held month on the page with recheck_all, plus those named in
    recheck."""
    held = set(held)
    floor = min_period or (min(held) if held else None)
    new, earlier = [], []
    for p in sorted(page):
        if p in held:
            continue
        ok = (p >= floor) if min_period else (floor is not None and p > floor)
        (new if ok else earlier).append(p)
    redo = []
    for p in sorted(page):
        if p not in held:
            continue
        link = page[p]
        if (recheck_all or p in set(recheck) or
                (link != tips.get(p) and link not in checked.get(p, ()))):
            redo.append(p)
    return new, redo, earlier


def cell_summary(cur, profile, by_period: dict, periods, against: str) -> dict:
    """Cells that differ between each month's records and what it is compared
    with (NULL equals NULL, NULL differs from 0): per column; NULL replacing a
    number and the reverse; 0 to NULL and NULL to 0; precision-only cells
    (below 1e-8) counted apart from the rest; total_bed_days_lost revisions
    above 50%; the five largest; per month the areas with a NULL replacing a
    number, with a bed-days revision above 50% and the zero/NULL cells (the
    stop conditions). Precision-only cells never count towards a stop.
    Read-only."""
    cols = profile.value_cols
    out = {"periods_changed": 0, "cells": 0, "precision_only": 0,
           "precision_max": Decimal(0), "by_col": {c: 0 for c in cols},
           "null_for_number": 0, "number_for_null": 0, "zero_null": 0,
           "over_50pct": 0, "areas_one_side": 0, "new_periods": 0,
           "new_cells": 0, "largest": [], "per_period": {}}
    big = []
    for p in periods:
        recs = by_period[p]
        old, _ = pe.stored(cur, profile, p, against)
        if not old:
            out["new_periods"] += 1
            out["new_cells"] += sum(1 for r in recs for c in cols
                                    if r[c] is not None)
            continue
        new = {pe._key(profile, r): tuple(r[c] for c in cols) for r in recs}
        out["areas_one_side"] += len(set(old) ^ set(new))
        changed = prec = zero_null = 0
        null_areas, big_areas = set(), set()
        for k in sorted(set(old) & set(new)):
            for c, o, n in zip(cols, old[k], new[k]):
                if (o is None) == (n is None) and (o is None or o == n):
                    continue
                if precision_only(o, n):
                    prec += 1
                    out["precision_max"] = max(out["precision_max"],
                                               abs(Decimal(o) - Decimal(n)))
                    continue
                changed += 1
                out["by_col"][c] += 1
                if n is None:
                    out["null_for_number"] += 1
                    null_areas.add(k)
                    if o == 0:
                        zero_null += 1
                elif o is None:
                    out["number_for_null"] += 1
                    if n == 0:
                        zero_null += 1
                elif c == BIG_COLUMN and o != 0:
                    rel = abs(Decimal(n) - Decimal(o)) / abs(Decimal(o))
                    if rel > Decimal("0.5"):
                        out["over_50pct"] += 1
                        big_areas.add(k)
                    big.append((rel, p, k, c, o, n))
        out["per_period"][p] = {"null_areas": len(null_areas),
                                "big_areas": len(big_areas),
                                "zero_null": zero_null,
                                "cells": changed, "precision": prec}
        out["precision_only"] += prec
        out["zero_null"] += zero_null
        if changed or prec:
            out["periods_changed"] += 1
        out["cells"] += changed
    big.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    out["largest"] = big[:5]
    return out


def stop_problems(summary: dict, acknowledged=()) -> dict:
    """{month: message} for months that break a stop condition and are not
    acknowledged: NULL replacing a number in more than MAX_NULL_AREAS areas,
    total bed days revised above 50% in more than MAX_BIG_REVISION_AREAS
    areas, a 0 becoming NULL or the reverse (rule 1.10). --acknowledge PERIOD
    releases the month."""
    out = {}
    for p, d in summary["per_period"].items():
        if p in acknowledged:
            continue
        msgs = []
        if d["null_areas"] > MAX_NULL_AREAS:
            msgs.append(f"NULL replaces a number in {d['null_areas']} areas "
                        f"(limit {MAX_NULL_AREAS})")
        if d["big_areas"] > MAX_BIG_REVISION_AREAS:
            msgs.append(f"total bed days revised above 50% in "
                        f"{d['big_areas']} areas (limit "
                        f"{MAX_BIG_REVISION_AREAS})")
        if d["zero_null"]:
            msgs.append(f"{d['zero_null']} cell(s) go from 0 to NULL or NULL "
                        "to 0 (rule 1.10; read them, then --acknowledge "
                        f"{p})")
        if msgs:
            out[p] = "STOP CONDITION: " + "; ".join(msgs)
    return out


def format_cell_summary(s: dict, against: str) -> str:
    what = "the LIVE table" if against == "live" else "the latest editions"
    cols = ", ".join(f"{c} {n}" for c, n in s["by_col"].items() if n) or "none"
    lines = [f"cells (compared with {what}): {s['cells']} changed in "
             f"{s['periods_changed']} month(s) (by column: {cols}); "
             f"precision-only (below 1e-8, max {s['precision_max']:.3E}) "
             f"{s['precision_only']}; NULL replacing a number "
             f"{s['null_for_number']}; a number replacing NULL "
             f"{s['number_for_null']}; 0 to NULL or NULL to 0 "
             f"{s['zero_null']}; bed-days revisions above 50% "
             f"{s['over_50pct']}; areas on one side only "
             f"{s['areas_one_side']}; new months {s['new_periods']} "
             f"({s['new_cells']} non-NULL cells)"]
    if s["largest"]:
        lines.append("largest bed-days revisions (relative):")
        for rel, p, k, c, o, n in s["largest"]:
            lines.append(f"  {p} {k} {c}: {o} -> {n} "
                         f"({'+' if n >= o else '-'}{rel * 100:.2f}%)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status, run log, commands
# ---------------------------------------------------------------------------

table_exists = pe.table_exists


def _conn(writing):
    return pe.connect(writing)


def status(cur, spec=SPEC) -> dict:
    return pe.status(cur, _profile(spec))


def format_status(st: dict) -> str:
    return pe.format_status(PROFILE, st)


def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '9a'). Called only on `sync-new --commit` and `load --commit`."""
    pe.log_run(cur, PROFILE, rows_written, notes, started_at)


def load_run_notes(stats: dict, held: list, page: dict, acked: dict) -> str:
    kinds = stats["kinds"]
    done = stats["periods"]
    by = {k: [p for p in done if kinds[p] == k]
          for k in ("new", "revised", "unchanged", LIVE_MISSING)}
    stored = by["new"] + by["revised"]
    head = ("Stored: " + "; ".join(f"{k} {', '.join(by[k])}"
                                   for k in ("new", "revised") if by[k])
            if stored else "Nothing new: no new month and no revision")
    if by[LIVE_MISSING]:
        head += (". Live rows inserted (no new edition) for editions months "
                 f"missing from live: {', '.join(by[LIVE_MISSING])}")
    if acked:
        head += ". ACKNOWLEDGED: " + "; ".join(f"{p}: {m}"
                                               for p, m in acked.items())
    span = f"{done[0]} .. {done[-1]}" if done else "none"
    return (f"{head}. Checked {len(done)} month(s) ({span}); unchanged "
            f"{len(by['unchanged'])}. Held latest "
            f"{max(held) if held else '-'}; page latest "
            f"{max(page) if page else '-'}. Rows stored: "
            f"{stats['stored_rows']} edition rows, {stats.get('live_rows', 0)} "  # not a source value
            "live rows inserted. A run that finds nothing new is logged "
            "because the check is the run.")


def cmd_ddl(args) -> int:
    return pe.run_ddl(_profile(SPEC), args, connect=_late("_conn"),
                      table_exists=_late("table_exists"))


def cmd_status(args) -> int:
    return pe.run_status(
        _profile(SPEC), args, connect=_late("_conn"),
        table_exists=_late("table_exists"),
        status=lambda cur: status(cur, SPEC),
        format_status=_late("format_status"))


def cmd_sync_new(args) -> int:
    return pe.run_sync_new(
        _profile(SPEC), args, connect=_late("_conn"),
        table_exists=_late("table_exists"),
        status=lambda cur: status(cur, SPEC),
        format_status=_late("format_status"))


def cmd_refresh_latest(args) -> int:
    return pe.run_refresh_latest(_profile(SPEC), args, connect=_late("_conn"),
                                 table_exists=_late("table_exists"))


def held_area_count(cur, spec=SPEC):
    """The most common per-month area count of the live table."""
    return core.modal_count(core.authority_counts(cur, spec))


def latest_live_areas(cur, spec=SPEC) -> set:
    """The areas of the live table's latest month (empty if none)."""
    cur.execute(f"SELECT DISTINCT utla_code FROM public.{spec.live_table} "
                f"WHERE {spec.period_col} = (SELECT MAX({spec.period_col}) "
                f"FROM public.{spec.live_table})")
    return {r[0] for r in cur.fetchall()}


def _derive_expected(cur, spec, override):
    derived = held_area_count(cur, spec)
    if override is not None and derived is not None and override != derived:
        print(f"NOTE: --expected-areas {override} overrides the held modal "
              f"count {derived}")
    expected = override if override is not None else derived
    if expected is None:
        halt("no live month to derive the area count from; give "
             "--expected-areas N")
    return expected


def tip_files(cur, spec, has_editions: bool) -> dict:
    """{period: the file the month's latest edition came from}; for a held
    month with no edition (or a NULL source_file) the live `source`."""
    out = {}
    cur.execute(f"SELECT {spec.period_col}, MAX(source) FROM "
                f"public.{spec.live_table} GROUP BY 1")
    for p, s in cur.fetchall():
        if s:
            out[pe._p(p)] = s
    if has_editions:
        cur.execute(f"SELECT DISTINCT ON ({spec.period_col}) "
                    f"{spec.period_col}, source_file FROM "
                    f"public.{spec.editions_table} ORDER BY "
                    f"{spec.period_col}, edition DESC")
        for p, s in cur.fetchall():
            if s:
                out[pe._p(p)] = s
    return out


def _read_month(period, path, link_name) -> tuple:
    """(rows, dropped, cover) of one month's file with identity checked."""
    p = Path(path)
    try:
        cover = read_cover(p)
    except ValueError as e:
        halt(f"{p.name}: {e}; nothing stored")
    problems = check_identity(period, cover, link_name)
    if problems:
        halt(f"file identity check failed for {link_name}, nothing stored: "
             + "; ".join(problems))
    try:
        rows, dropped = parse_workbook(p)
    except ValueError as e:
        halt(f"{p.name}: {e}; nothing stored")
    if not rows:
        halt(f"{p.name}: no UTLA Aggregate rows; nothing stored")
    return rows, dropped, cover


def _iso_month(text: str) -> str:
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})(?:-01)?", text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"not a first-of-month date YYYY-MM-01 (or YYYY-MM): {text!r}")
    return f"{m.group(1)}-{m.group(2)}-01"


def cmd_load(args) -> int:
    spec = SPEC
    base = _profile(spec)
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            has_ed = table_exists(cur, spec.editions_table)
            has_ledger = has_ed and table_exists(
                cur, pe.file_checks_table(base))
            if writing:
                if not has_ed:
                    halt(pe.no_table(base))
                if not has_ledger:
                    halt(f"{pe.file_checks_table(base)} does not exist; run "
                         "`ddl --commit` (it creates the file-check ledger)")
                _, new_live, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new_live:
                    halt(f"live months with no editions "
                         f"{[pe._p(x) for x in new_live]}; run sync-new first "
                         "so the history starts from what is held")
            against = "editions" if has_ed else "live"
            if not has_ed:
                print(f"NOTE: {spec.editions_table} does not exist yet; this "
                      "preview compares each month with the LIVE table "
                      f"{spec.live_table}")
            held = pe.held_periods(cur, base, has_ed)
            print("held months: " + (f"{held[0]} .. {held[-1]} ({len(held)})"
                                     if held else "none"))
            if not held and args.min_period is None:
                halt("nothing is held, so no window can be planned; give "
                     "--min-period YYYY-MM-01 (and --expected-areas N)")
            expected = _derive_expected(cur, spec, args.expected_areas)
            required = latest_live_areas(cur, spec)
            tips = tip_files(cur, spec, has_ed)
            checked = pe.checked_files(cur, base) if has_ledger else {}
            stranded = (pe.live_missing_periods(cur, base) if has_ed else [])

            html = ""
            if args.file:
                path_given = Path(args.file)
                fp = period_from_link(path_given.name)
                if fp is None:
                    halt(f"--file {path_given.name}: no month in the name; "
                         "nothing stored")
                page = {fp: path_given.name}
                print("file given: nothing downloaded")
            else:
                html = fetch_page()
                links = find_month_links(html)
                if not links:
                    halt("no monthly DRD webfile links found on the "
                         f"publication page {PAGE}; nothing stored")
                page = {}
                for p, urls in sorted(links.items()):
                    try:
                        page[p] = choose_link(urls)
                    except ValueError as e:
                        halt(f"{p}: {e}")
                print(f"page: {len(page)} month(s) {min(page)} .. "
                      f"{max(page)}")
                if held and max(page) < max(held):
                    msg = (f"the page's latest month is {max(page)[:7]} but "
                           f"{max(held)[:7]} is already held")
                    if args.allow_older_file:
                        print(f"NOTE: --allow-older-file given; {msg}")
                    else:
                        halt(msg + "; the page looks older than what is held. "
                             "If this is deliberate, re-run with "
                             "--allow-older-file")
                for note in csv_reissue_notes(html, page):
                    print(note)

            new_p, recheck, earlier = plan_months(
                held, page, tips, checked, recheck_all=args.recheck_all,
                recheck=args.recheck or (), min_period=args.min_period)
            if args.file:
                recheck = [p for p in page if p in held]
            recheck = sorted(set(recheck) | {p for p in stranded
                                             if p in page})
            for p in args.recheck or ():
                if p not in held:
                    halt(f"--recheck {p}: not a held month")
                if p not in page:
                    halt(f"--recheck {p}: not on the page; nothing to read")
            print(f"planned: new {', '.join(new_p) or 'none'}; recheck "
                  f"{len(recheck)} held month(s)"
                  + (f" ({recheck[0]} .. {recheck[-1]})" if recheck else "")
                  + (f"; editions months missing from live "
                     f"{', '.join(p for p in stranded if p in page)}"
                     if any(p in page for p in stranded) else ""))
            if earlier:
                print(f"ignored, earlier than the earliest held month "
                      f"{held[0] if held else '-'} (give --min-period to "
                      f"widen): {', '.join(earlier)}")
            periods = sorted(set(new_p) | set(recheck))
            ack = set(args.acknowledge or ())
            stray = sorted(ack - set(periods))
            if stray:
                halt(f"--acknowledge {stray}: not months of this run "
                     f"({', '.join(periods) or 'none'})")
            # the older-file guard, per month
            for p in recheck:
                if not args.file and link_older(page[p], tips.get(p)):
                    msg = (f"{p}: the page offers {_name(page[p])} but the "
                           f"month's latest file is {_name(tips[p])}")
                    if args.allow_older_file:
                        print(f"NOTE: --allow-older-file given; {msg}")
                    else:
                        halt(msg + "; storing it would record an older file "
                             "as a revision. If this is deliberate, re-run "
                             "with --allow-older-file")
            print(f"expected areas per month: {expected}"
                  + (" (--expected-areas)" if args.expected_areas is not None
                     else " (held modal count)")
                  + f"; every area of the live table's latest month "
                  f"({len(required)}) required")
            if not periods:
                print("nothing to fetch")
                rc = 0
            else:
                rc = _run_months(args, conn, cur, spec, periods, page, tips,
                                 expected, required, against, held, ack,
                                 new_p)
        conn.rollback()  # commit mode has committed month by month already
        if not writing:
            print("PREVIEW: nothing written (use --commit or --simulate)")
        elif args.simulate:
            print("SIMULATION: ROLLED BACK (nothing persisted)")
        return rc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_months(args, conn, cur, spec, periods, page, tips, expected,
                required, against, held, ack, new_p) -> int:
    """Fetch, read and check each planned month, then compare and store."""
    started = datetime.now(timezone.utc)
    infos, rows_by, covers, dropped_all = {}, {}, {}, {}
    for p in periods:
        url = page[p]
        if args.file:
            path = Path(args.file)
            source_file = path.name
            print(f"NOTE: --file: {p} is stored with source_file "
                  f"{source_file} (no link URL known)")
        else:
            path = fetch_month(url, RAW_DIR)
            source_file = url
        link_name = _name(source_file)
        rows, dropped, cover = _read_month(p, path, link_name)
        rows_by[p], covers[p], dropped_all[p] = rows, cover, dropped
        infos[p] = (source_file, path, content_sha256(path))
        print(f"  {p}: {link_name}: {len(rows)} UTLA rows; dropped "
              f"{dropped or 'none'}; published {cover['published'] or '-'}, "
              f"revised {cover['revised'] or '-'}")
    seen = {p: {r["utla_code"] for r in rows_by[p]} for p in periods}
    try:
        recode_map, problems = geography.resolve(
            cur, RUN_SOURCE,
            set().union(*seen.values()) if seen else (), by_period=seen)
    except ValueError as e:
        halt(f"Barnsley/Sheffield geography: {e}; nothing stored")
    if problems:
        halt("Barnsley/Sheffield geography check failed: "
             + "; ".join(problems))
    note = geography.confirm_note(RUN_SOURCE)
    if note:
        print(note)
    valid = valid_codes(cur)
    unknown = sorted({recode_map.get(c, c) for s in seen.values() for c in s}
                     - valid)
    if unknown:
        halt("unresolved UTLA codes " + str([f"UNEXPLAINED {c}"
                                             for c in unknown])
             + "; explain them in utla_lad_mapping before loading; nothing "
             "stored")
    by_period = {}
    for p in periods:
        try:
            recs = build_records(rows_by[p], recode_map, p)
        except ValueError as e:
            halt(f"{p}: {e}; nothing stored")
        for r in recs:
            r["source"] = infos[p][0]
        by_period[p] = recs
    profile = run_profile(spec, expected, required)
    problems = period_problems(by_period, periods, expected, required)
    for p, msg in problems.items():
        print(f"  {p}: REJECTED, not stored: {msg}")
    ok = [p for p in periods if p not in problems]
    summary = cell_summary(cur, profile, by_period, ok, against)
    print(format_cell_summary(summary, against))
    all_stops = stop_problems(summary)
    stops = stop_problems(summary, ack)
    acked = {p: all_stops[p] for p in all_stops if p in ack}
    for p, msg in stops.items():
        print(f"  {p}: REJECTED, not stored: {msg}")
    for p, msg in acked.items():
        print(f"  {p}: ACKNOWLEDGED (--acknowledge): {msg}")
    problems.update(stops)
    ok = [p for p in ok if p not in stops]
    labels = {}
    for p in ok:
        source_file, path, sha = infos[p]
        d = summary["per_period"].get(p)
        kind = ("Revised file" if covers[p]["revised"] else "original file")
        if d and d["cells"] == 0 and d["precision"]:
            kind += ", precision-only difference from the held values"
        labels[p] = FileInfo(source_file, path, sha, release_label(
            kind, covers[p], sha, acked.get(p, "").removeprefix(
                "STOP CONDITION: ")))
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    rc = 0
    if ok:
        rc = pe.load_periods(
            cur, profile, ok, lambda p: by_period[p], date.today(),
            args.commit, simulate=args.simulate, against=against,
            stats=stats, apply=lambda c, prof, p, recs, *, fetched_on,
            source_file=None: _late("apply_period")(
                c, prof, p, recs, fetched_on=fetched_on, info=labels[p]))
    else:
        print("nothing to compare")
    if problems:
        print(f"REJECTED {len(problems)} month(s), not stored: "
              f"{', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        if rc == 0:
            log_run(cur, stats["stored_rows"] + stats["live_rows"],  # not a source value
                    load_run_notes(stats, held, page, acked), started)
            conn.commit()
            print("pipeline_run_log row written")
        else:
            print("pipeline_run_log: no row written (the run failed or "
                  "rejected a month; months committed before are in the "
                  "editions table)")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S9a NHS England DRD: editions "
                                 "loader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table and the "
                       "file-check ledger (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("sync-new", help="edition 1 'as loaded' for live months "
                       "with no editions (preview by default)")
    p.add_argument("--expected-areas", "--expected-authorities", type=int,
                   dest="expected_authorities", metavar="N",
                   help="areas per month; only when nothing can be derived "
                   "(no month has editions yet)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_sync_new)
    p = sub.add_parser("load", help="read each month's current webfile, "
                       "compare and store months (preview by default)")
    p.add_argument("--file", metavar="FILE",
                   help="one local webfile (nothing is downloaded; identity "
                   "is read from the file)")
    p.add_argument("--min-period", type=_iso_month, metavar="YYYY-MM-01",
                   help="load every month on the page from this month "
                   "(required when nothing is held)")
    p.add_argument("--recheck-all", action="store_true",
                   help="read every held month on the page again")
    p.add_argument("--recheck", action="append", metavar="PERIOD",
                   type=_iso_month,
                   help="read this held month again (repeatable)")
    p.add_argument("--expected-areas", type=int, metavar="N",
                   help="areas per month (default the held modal count)")
    p.add_argument("--allow-older-file", action="store_true",
                   help="load a file ranking below the month's latest file, "
                   "or when the page's latest month is earlier than the held "
                   "latest (default: halt)")
    p.add_argument("--acknowledge", action="append", metavar="PERIOD",
                   type=_iso_month,
                   help="release a month stopped by a stop condition or a "
                   "0/NULL change after reading the preview (repeatable)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each month's latest "
                       "edition into the live table, source included "
                       "(preview by default)")
    pe.mode_parser(p)
    p.add_argument("--accept-drift", action="append", metavar="PERIOD",
                   help="overwrite this month (YYYY-MM-DD) although its live "
                   "rows equal no stored edition (repeatable)")
    p.set_defaults(func=cmd_refresh_latest)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
