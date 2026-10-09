"""S9b (NHS England MHSDS, measure MHS26: days of delayed discharge for
patients clinically ready for discharge, by local authority): edition history
and loader on the period-editions engine.

nhs_mh_crfd holds one row per local authority and month (measure_id always
MHS26): the latest layer. This module owns nhs_mh_crfd_editions, which keeps
every file of a month that differed from the one before, so a revision never
overwrites what was held. The machinery is editions_core driven by SPEC,
through period_editions (pe) bound to PROFILE. The pure part (pages, links,
streaming and parsing the MHSDS data file, records, identity) comes first and
keeps the parsing behaviour of the retired s9b_crfd_build.py; the loader
follows. Importing this module needs no database, no network and no key.

NHS England publishes ONE FILE PER MONTH on that month's publication page,
and replaces it in two ways: a reissued Performance file ('v2', 'v3') and,
after the financial year closes, a year-end Final file beside the Performance
file. The old loader excluded Final files on purpose; this one takes the
highest-ranking data file of each month (Final above Performance, then the
version), compares it with the file the month's latest edition came from,
and records every file read in the file-check ledger
(nhs_mh_crfd_editions_file_checks) so an unchanged reissue is not downloaded
again. --recheck-all and --recheck PERIOD re-read held months.

The files are large (a zip of 2.5-11 MB holding one CSV of about 175 MB, or
a plain CSV of about 70 MB): a download is written to disk in chunks, and the
CSV (or the largest CSV member of the zip) is read a line at a time. A file
is never held in memory.

Blanks and zeros (docs/RULES.md rule 1): '*' (small-number suppression) and a
blank are NULL, never 0; any other value that is not a whole number halts; 0
to NULL or NULL to 0 against the latest edition needs --acknowledge PERIOD.

Identity (rule 3) is read from the file: every MHS26 row's
REPORTING_PERIOD_START (written 2023-04-01, 01/04/2024, 01-04-2025 or, in
the June 2024 Final, 01/06/24) must be
the month and REPORTING_PERIOD_END its last day, and STATUS (stripped; one
file has 'Performance ') must be the link's kind, one of each per file.

Barnsley and Sheffield (rule 4): source 9b is declared 'mixed' in
scripts/geography.py (the form changes from file to file); either form is
accepted, a month (one file) must never carry both forms of one area, and
the recode map comes only from geography.resolve. Codes are checked against
la_boundaries.

Stop conditions (enforced in load: a stopped month is REJECTED, not stored,
exit 1; --acknowledge PERIOD releases it after the preview has been read):
  - Performance to Final (the publisher's year-end refresh, which changes
    100-150 areas a month): reported in full; stops only if the national
    total of non-NULL values moves by more than 50%.
  - any other revision (a reissued Performance or Final file): NULL
    replacing a number in more than 5 areas, or a value revised by more than
    50% in more than 10 areas.
  - always: 0 to NULL or NULL to 0 (rule 1.10).
A month whose area count is not the held count (296), that misses an area of
the held latest month, or carries a negative value is REJECTED.

Live `source` stays the file URL (the views and check_sources.py read it); an
edition's source_file is that URL exactly.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back):
    python scripts/s9b_crfd_editions.py ddl [--commit | --simulate]
    python scripts/s9b_crfd_editions.py status
    python scripts/s9b_crfd_editions.py sync-new [--expected-areas N]
                                                 [--commit | --simulate]
    python scripts/s9b_crfd_editions.py load [--file F] [--min-period D]
                                             [--recheck-all] [--recheck P ...]
                                             [--expected-areas N]
                                             [--allow-older-file]
                                             [--acknowledge P ...]
                                             [--commit | --simulate]
        # pages = the month's publication page for every month from the
        # earliest held (or --min-period) to this month; new = months with a
        # data file, not held; recheck = held months whose chosen file is
        # neither the latest edition's file nor already in the ledger, plus
        # --recheck / --recheck-all, plus editions months missing from live.
        # Downloads go to data/raw/s9b_mhsds/ (also in a preview; a preview
        # writes nothing to the database).
    python scripts/s9b_crfd_editions.py refresh-latest [--commit | --simulate]
                                                       [--accept-drift PERIOD]
"""
import argparse
import calendar
import csv
import dataclasses
import hashlib
import html as html_lib
import io
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

MIN_PERIOD = "2023-04-01"
LIVE = "nhs_mh_crfd"
TABLE = "nhs_mh_crfd_editions"
RUN_AGENT = "Source 9b - MHSDS MHS26 CRFD"
RUN_SOURCE = "9b"
SOURCE_FILE = "NHS England MHSDS monthly data file"
LIVE_MISSING = pe.LIVE_MISSING

PAGE_BASE = ("https://digital.nhs.uk/data-and-information/publications/"
             "statistical/mental-health-services-monthly-statistics/")
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s9b_mhsds"      # git-ignored
USER_AGENT = ("ucws-pipeline S9b loader (read-only download of the public "
              "NHS England MHSDS monthly files)")
MIN_FILE_BYTES = 100 * 1024
CHUNK = 1 << 20

MEASURE = "MHS26"
BREAKDOWN = "Local Authority of Responsibility or Residence"
REQUIRED_COLUMNS = ("REPORTING_PERIOD_START", "REPORTING_PERIOD_END",
                    "STATUS", "BREAKDOWN", "PRIMARY_LEVEL",
                    "PRIMARY_LEVEL_DESCRIPTION", "SECONDARY_LEVEL",
                    "MEASURE_ID", "MEASURE_NAME", "MEASURE_VALUE")
LA_CODE_RE = re.compile(r"E0[6-9][0-9]{6}")
E10_RE = re.compile(r"E10[0-9]{6}")
DROPPED = ("E10", "UNKNOWN")      # counties (overlap their districts), unknown
MARKERS = ("*", "")                # small-number suppression, blank: NULL
WHOLE_RE = re.compile(r"-?[0-9]+")
VALUE_COLUMNS = ("measure_value",)

KINDS = ("Performance", "Final")
FINAL = "Final"
PERFORMANCE = "Performance"
EXCLUDED_WORDS = ("rstr", "restrictive", "oaps", "out of area", "4ww",
                  "ascof", "time_series", "time series", "dq_", "dq ",
                  "reference tables", "referral spells")

MAX_NULL_AREAS = 5            # other revisions: NULL replacing a number
MAX_BIG_REVISION_AREAS = 10   # other revisions: value revised above 50%
MAX_FINAL_TOTAL_MOVE = 0.5    # Performance to Final: national total moves

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_NAMES = {}
for _i, _m in enumerate(_MONTHS, 1):
    _MONTH_NAMES[_m] = _i
    _MONTH_NAMES[_m[:3]] = _i
_MONTH_NAMES["sept"] = 9
_SHORT_FORM = re.compile(
    r"MHSDS Data_([A-Za-z]{3,9})(Prf|Perf|Final)_+([0-9]{4})"
    r"(?:[ _]?v([0-9]+))?\.(zip|csv)", re.I)
_LONG_FORM = re.compile(
    r"MHSDS Monthly (Performance|Final) ([A-Za-z]{3,9}) ([0-9]{4}) MHSDS "
    r"Data File(?:[ _]?v([0-9]+))?\.(zip|csv)", re.I)
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# y-m-d, d/m/y and d-m-y; day and month with or without a leading zero, the
# year with four digits or (seen in the June 2024 Final: '01/06/24') two
_DAY_FORMS = (re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})"),
              re.compile(r"([0-9]{1,2})/([0-9]{1,2})/([0-9]{4}|[0-9]{2})"),
              re.compile(r"([0-9]{1,2})-([0-9]{1,2})-([0-9]{4}|[0-9]{2})"))


# ---------------------------------------------------------------------------
# Pages and links
# ---------------------------------------------------------------------------

def _name(url: str) -> str:
    return unquote(urlsplit(url or "").path.rsplit("/", 1)[-1])


def publication_slugs(period: str) -> list:
    """The publication page slugs for a month, in the order to try:
    'performance-{month}-{year}', then the form used before October 2023,
    'performance-{month}-provisional-{next month}-{year}'."""
    d = date.fromisoformat(period)
    month = _MONTHS[d.month - 1]
    nxt = _MONTHS[d.month % 12]
    return [f"performance-{month}-{d.year}",
            f"performance-{month}-provisional-{nxt}-{d.year}"]


def _link_info(url):
    """(period, kind, version) of an MHSDS data file link or name, or None
    for any other file (the Rstr, OAPs, 4WW, ASCOF, Time_Series and DQ
    files, the provisional 'MayP' files and everything else)."""
    name = _name(url)
    low = name.lower()
    if any(w in low for w in EXCLUDED_WORDS):
        return None
    m = _SHORT_FORM.fullmatch(name)
    if m:
        mon, kind, year, ver = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        m = _LONG_FORM.fullmatch(name)
        if not m:
            return None
        kind, mon, year, ver = m.group(1), m.group(2), m.group(3), m.group(4)
    mo = _MONTH_NAMES.get(mon.lower())
    if mo is None:
        return None
    kind = FINAL if kind.lower() == "final" else PERFORMANCE
    return f"{int(year):04d}-{mo:02d}-01", kind, int(ver) if ver else 1


def period_from_link(url) -> "str | None":
    """'YYYY-MM-01' named by an MHSDS data file link or name; None if it is
    not a data file name."""
    info = _link_info(url if isinstance(url, str) else "")
    return info[0] if info else None


def find_data_links(html: str) -> list:
    """The MHSDS data file links on a publication page (both naming forms:
    'MHSDS Data_AprPerf_2026 v2.zip', 'MHSDS Data_AprFinal__2025.zip',
    'MHSDS Monthly Performance July 2025 MHSDS Data File.zip'), each once, in
    page order. Every other file is left out."""
    out = []
    for href in re.findall(r"""href=["']([^"']+)["']""", html or "", re.I):
        url = html_lib.unescape(href)
        if _link_info(url) is None:
            continue
        if url not in out:
            out.append(url)
    return out


def classify_link(url: str) -> tuple:
    """(kind, version): kind 'Final' or 'Performance' (Prf, Perf), version
    1 unless the name carries vN. ValueError for a non-data file."""
    info = _link_info(url)
    if info is None:
        raise ValueError(f"not an MHSDS data file link: {_name(url)!r}")
    return info[1], info[2]


def link_rank(url: str) -> tuple:
    """(int(Final), version): Final above Performance, then the
    version."""
    kind, version = classify_link(url)
    return (int(kind == FINAL), version)


def link_older(chosen: str, tip_file: "str | None") -> bool:
    """True if the chosen link ranks below the file a month's latest edition
    came from (a Performance file after a Final, v1 after v2). A tip file
    that is not a data file name (an offline --file) is never older."""
    if not tip_file or _link_info(tip_file) is None:
        return False
    return link_rank(chosen) < link_rank(tip_file)


def file_older(kind: str, version: int, tip_file: "str | None") -> bool:
    """True if a --file of this kind (read from the file's own STATUS) and
    version (from its name, 1 if the name carries none) ranks below the file
    a month's latest edition came from. A tip that is not a data file name is
    never newer."""
    if not tip_file or _link_info(tip_file) is None:
        return False
    return (int(kind == FINAL), version) < link_rank(tip_file)


def choose_link(links) -> str:
    """The link of highest link_rank; two that tie raise ValueError naming
    both (no winner is guessed)."""
    links = list(dict.fromkeys(links))
    if not links:
        raise ValueError("no link to choose from")
    best = max(link_rank(u) for u in links)
    top = [u for u in links if link_rank(u) == best]
    if len(top) > 1:
        raise ValueError("two data files for one month rank the same "
                         f"{best}: {top[0]} and {top[1]}; refusing to choose")
    return top[0]


# ---------------------------------------------------------------------------
# Reading a data file (streamed)
# ---------------------------------------------------------------------------

def _csv_member(z: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    members = [i for i in z.infolist()
               if not i.is_dir() and i.filename.lower().endswith(".csv")]
    if not members:
        raise ValueError(f"{name}: no CSV inside the archive "
                         f"({[i.filename for i in z.infolist()]})")
    return max(members, key=lambda i: i.file_size)


def _rows_of(text, name):
    reader = csv.reader(text)
    try:
        header = [h.strip().upper() for h in next(reader)]
    except StopIteration:
        raise ValueError(f"{name}: empty file") from None
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"{name}: header missing {missing} (header "
                         f"{header[:12]})")
    width = len(header)
    for n, row in enumerate(reader, 2):
        if not row or (len(row) == 1 and not row[0].strip()):
            continue
        if len(row) != width:
            raise ValueError(f"{name}: line {n} has {len(row)} fields, the "
                             f"header {width}")
        yield dict(zip(header, row))


def iter_csv_rows(path):
    """Each data row of a plain CSV, or of the largest CSV member of a zip,
    as {HEADER (stripped, upper case): text}, read a line at a time (a zip
    member through ZipFile.open, never ZipFile.read). Decoded as UTF-8 with
    a BOM allowed; an undecodable byte (the April 2023 file has 0xA0) becomes
    U+FFFD. ValueError on a missing column or a row of the wrong width."""
    p = Path(path)
    if zipfile.is_zipfile(p):
        with zipfile.ZipFile(p) as z:
            info = _csv_member(z, p.name)
            with z.open(info) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig",
                                        errors="replace", newline="")
                yield from _rows_of(text, f"{p.name}:{info.filename}")
    else:
        with open(p, encoding="utf-8-sig", errors="replace",
                  newline="") as text:
            yield from _rows_of(text, p.name)


def _parse_day(text: str) -> str:
    """A REPORTING_PERIOD_* value as ISO 'YYYY-MM-DD'. Forms seen in the
    files: '2023-04-01', '01/04/2024', '01-04-2025', '01/06/24' and
    '1/6/2024' (a two-digit year is 20yy). ValueError otherwise."""
    s = (text or "").strip()
    for i, rx in enumerate(_DAY_FORMS):
        m = rx.fullmatch(s)
        if m:
            y, mo, d = ((m.group(1), m.group(2), m.group(3)) if i == 0
                        else (m.group(3), m.group(2), m.group(1)))
            year = int(y)
            if len(y) == 2:
                year += 2000
            try:
                return date(year, int(mo), int(d)).isoformat()
            except ValueError:
                break
    raise ValueError(f"not a date in a known form: {text!r}")


def _value(raw: str, where: str):
    """MEASURE_VALUE -> int or None. '*' and blank are NULL; a whole number
    is an int; anything else raises (no silent NULL, never 0)."""
    s = (raw or "").strip()
    if s in MARKERS:
        return None
    if WHOLE_RE.fullmatch(s):
        return int(s)
    raise ValueError(f"{where}: MEASURE_VALUE {raw!r} is neither a whole "
                     "number nor a known marker ('*' or blank)")


def parse_file(path) -> tuple:
    """(rows, meta) of one data file, streamed. rows: the MHS26 rows of the
    breakdown exactly 'Local Authority of Responsibility or Residence' with
    SECONDARY_LEVEL NONE and an E06-E09 code, as {lad24cd (the publisher's
    own code, not recoded), la_name, measure_name, measure_value}. E10
    counties and UNKNOWN are dropped and counted; any other code raises.
    meta: periods and period_ends (ISO, from every MHS26 row, any of the
    date forms of _parse_day), statuses (stripped), statuses_raw, measure_names (of
    the kept rows), dropped {E10: n, UNKNOWN: n}, mhs26_rows."""
    name = Path(path).name
    rows = []
    periods, ends, statuses, raw_st, names = set(), set(), set(), set(), set()
    dropped = {}
    n = 0
    for r in iter_csv_rows(path):
        if r["MEASURE_ID"].strip() != MEASURE:
            continue
        n += 1
        periods.add(r["REPORTING_PERIOD_START"].strip())
        ends.add(r["REPORTING_PERIOD_END"].strip())
        raw_st.add(r["STATUS"])
        statuses.add(r["STATUS"].strip())
        if (r["BREAKDOWN"].strip() != BREAKDOWN
                or r["SECONDARY_LEVEL"].strip().upper() != "NONE"):
            continue
        code = r["PRIMARY_LEVEL"].strip()
        if code == "UNKNOWN" or E10_RE.fullmatch(code):
            key = "UNKNOWN" if code == "UNKNOWN" else "E10"
            dropped[key] = dropped.get(key, 0) + 1
            continue
        if not LA_CODE_RE.fullmatch(code):
            raise ValueError(f"{name}: MHS26 local authority row with code "
                             f"{code!r}, expected E06-E09 (or E10 / UNKNOWN, "
                             "dropped)")
        mname = r["MEASURE_NAME"].strip() or None
        names.add(mname)
        rows.append({"lad24cd": code,
                     "la_name": r["PRIMARY_LEVEL_DESCRIPTION"].strip() or None,
                     "measure_name": mname,
                     "measure_value": _value(r["MEASURE_VALUE"],
                                             f"{name} {code}")})
    try:
        meta_periods = sorted({_parse_day(x) for x in periods})
        meta_ends = sorted({_parse_day(x) for x in ends})
    except ValueError as e:
        raise ValueError(f"{name}: REPORTING_PERIOD {e}") from None
    meta = {"periods": meta_periods, "period_ends": meta_ends,
            "statuses": sorted(statuses), "statuses_raw": sorted(raw_st),
            "measure_names": sorted(x for x in names if x),
            "dropped": dropped, "mhs26_rows": n}
    return rows, meta


def check_identity(period: str, kind: str, meta: dict, link_name: str) -> list:
    """Problems (empty = fine): every MHS26 row's REPORTING_PERIOD_START is
    the month and REPORTING_PERIOD_END its last day (one of each); STATUS
    (stripped) is one value and equals the link's kind; a link name that
    names a month and kind names this month and kind."""
    problems = []
    d = date.fromisoformat(period)
    last = date(d.year, d.month,
                calendar.monthrange(d.year, d.month)[1]).isoformat()
    if not meta.get("mhs26_rows"):
        problems.append(f"{link_name}: no {MEASURE} rows in the file")
    if meta.get("periods") != [period]:
        problems.append(f"{link_name}: REPORTING_PERIOD_START is "
                        f"{meta.get('periods')}, expected [{period}]")
    if meta.get("period_ends") != [last]:
        problems.append(f"{link_name}: REPORTING_PERIOD_END is "
                        f"{meta.get('period_ends')}, expected [{last}]")
    if meta.get("statuses") != [kind]:
        problems.append(f"{link_name}: STATUS is {meta.get('statuses')}, the "
                        f"link is a {kind} file")
    info = _link_info(link_name)
    if info is not None:
        if info[0] != period:
            problems.append(f"{link_name} names {info[0]}, not {period}")
        if info[1] != kind:
            problems.append(f"{link_name} is a {info[1]} file, not {kind}")
    return problems


def build_records(rows, recodes, period) -> list:
    """Rows -> records for one month: lad24cd through recodes (publisher code
    -> canonical; any other unchanged), measure_id 'MHS26', reporting_period
    = period, sorted by code. ValueError on a duplicate key (two source codes
    meeting, or a repeated row): no winner is chosen."""
    out, seen = [], {}
    for r in rows:
        code = recodes.get(r["lad24cd"], r["lad24cd"])
        if code in seen:
            raise ValueError(f"duplicate record for lad24cd {code}, "
                             f"measure_id {MEASURE}, period {period}: source "
                             f"codes {seen[code]} and {r['lad24cd']}; refusing "
                             "to choose a winner")
        seen[code] = r["lad24cd"]
        out.append({**r, "lad24cd": code, "measure_id": MEASURE,
                    "reporting_period": period})
    out.sort(key=lambda x: x["lad24cd"])
    return out


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes (read in blocks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

# Live table (checked 2026-10-09 in information_schema): reporting_period
# date, lad24cd varchar and measure_id varchar (NOT NULL, primary key
# together), la_name varchar, measure_name text, measure_value integer,
# source text, loaded_at timestamptz.
SPEC = core.EditionSpec(
    name="s9b",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd", "measure_id"),
    period_col="reporting_period",
    value_cols=(("measure_value", "integer"),),
    extra_cols=(("la_name", "varchar"), ("measure_name", "text")),
    refresh_cols=("measure_value", "la_name", "measure_name", "source"),
    refresh_from=(("source", "source_file"),),
    as_loaded_source_col="source",
    refresh_source_whole_period=True,   # one source per refreshed month
    key_types=(("reporting_period", "date NOT NULL"),
               ("measure_id", "varchar NOT NULL")),
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S9b release label comes from the file; store "
                     "through apply_period(...)")


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s9b_crfd_editions, name, ...) reaches the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUE_COLUMNS, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S9b MHSDS MHS26 CRFD",
    default_source_file=SOURCE_FILE,
    expected_areas=None,   # derived at run time from the held modal count
    release_label=_no_label, file_checks=True,
    savepoint="s9b_period",
    example_label="(measure_value)")


def _profile(spec) -> pe.Profile:
    return PROFILE.with_spec(spec)


def check_period(records: list, period: str, expected_areas,
                 required_areas) -> None:
    """ValueError unless the month is whole: an ISO date, every record of it,
    no key repeated, no negative value, exactly expected_areas areas (None:
    not checked) and every area of required_areas (the live table's latest
    month)."""
    if not isinstance(period, str) or not _ISO_DAY.fullmatch(period):
        raise ValueError(f"not an ISO date period: {period!r}")
    seen = set()
    for r in records:
        if r.get("reporting_period") != period:
            raise ValueError(f"{period}: a record of period "
                             f"{r.get('reporting_period')!r}")
        k = (r["lad24cd"], r["measure_id"])
        if k in seen:
            raise ValueError(f"{period}: {k} appears twice")
        seen.add(k)
        v = r["measure_value"]
        if v is not None and v < 0:
            raise ValueError(f"{period}: {r['lad24cd']} measure_value is "
                             f"negative ({v})")
    areas = {k[0] for k in seen}
    n = len(areas)
    if expected_areas is not None and n != expected_areas:
        why = ("a short month is never stored" if n < expected_areas else
               "more areas than held; review them, then give "
               "--expected-areas N")
        raise ValueError(f"{period}: {n} areas, expected {expected_areas}; "
                         f"{why}")
    missing = sorted(set(required_areas) - areas)
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
    """The month's live rows with la_name, measure_name and `source` (the
    file URL, taken from each record's 'source' key); loaded_at takes its
    default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = (tuple(spec.key_cols) + (spec.period_col,)
            + tuple(profile.value_cols) + ("la_name", "measure_name",
                                           "source"))
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r[c] for c in profile.value_cols)
         + (r.get("la_name"), r.get("measure_name"), r.get("source"))
         for r in records],
        page_size=1000)


@dataclass(frozen=True)
class FileInfo:
    """One month's file: its link (the edition's source_file), the path read,
    the file sha256 and the release label."""
    url: str
    path: Path
    sha256: str
    label: str


def release_label(period: str, kind: str, version: int, meta: dict,
                  sha: str, note: str = "", acknowledged: str = "") -> str:
    return (f"NHS England MHSDS {kind} data file {period[:7]} (v{version}); "
            f"STATUS {'/'.join(meta.get('statuses') or ['-'])}; file sha256 "
            f"{sha[:16]}" + (f"; {note}" if note else "")
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
# Files: pages and downloads
# ---------------------------------------------------------------------------

def _session(session):
    if session is not None:
        return session
    import requests
    return requests


def fetch_page(period: str, session=None) -> "str | None":
    """The month's publication page HTML (read-only GETs), trying each of
    publication_slugs(period); None if every slug is 404 (not published).
    Any other failure halts."""
    s = _session(session)
    for slug in publication_slugs(period):
        url = PAGE_BASE + slug
        try:
            r = s.get(url, headers={"User-Agent": USER_AGENT}, timeout=120)
        except Exception as e:  # noqa: BLE001 (any failure halts)
            halt(f"GET {url} failed: {e}")
        if getattr(r, "status_code", 200) == 404:
            continue
        try:
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            halt(f"GET {url} failed: {e}")
        return r.text
    return None


def _check_data_file(path: Path, name: str) -> None:
    """ValueError unless path is a zip holding a CSV, or a CSV, whose header
    carries the MHSDS columns (only the header is read)."""
    rows = iter_csv_rows(path)
    try:
        next(rows, None)
    finally:
        rows.close()


def fetch_month(url, dest=None, session=None) -> Path:
    """Download one month's data file to dest (default data/raw/s9b_mhsds/)
    under the link's file name, in chunks (never held in memory), and return
    the path to read. Checked before it is kept: more than MIN_FILE_BYTES
    and a zip holding a CSV, or a CSV, with the MHSDS header. A same-named
    file with the same content is kept as it is; one with different content
    is never replaced: the new download is saved beside it as
    <stem>-<sha8><ext> and that is the one read."""
    dest = Path(dest) if dest is not None else RAW_DIR
    dest.mkdir(parents=True, exist_ok=True)
    name = _name(url)
    target = dest / name
    tmp = dest / (Path(name).stem + ".download.tmp" + Path(name).suffix)
    h = hashlib.sha256()
    size = 0
    try:
        r = _session(session).get(url, headers={"User-Agent": USER_AGENT},
                                  timeout=600, stream=True)
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if chunk:
                    f.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
        if hasattr(r, "close"):
            r.close()
    except Exception as e:  # noqa: BLE001 (any failure halts)
        if tmp.exists():
            tmp.unlink()
        halt(f"GET {url} failed, nothing kept: {e}")
    if size <= MIN_FILE_BYTES:
        with open(tmp, "rb") as f:
            head = f.read(100)
        tmp.unlink()
        halt(f"{url}: {size} bytes, expected more than {MIN_FILE_BYTES}; "
             f"nothing kept; saw {head!r}")
    try:
        _check_data_file(tmp, name)
    except Exception as e:  # noqa: BLE001
        tmp.unlink()
        halt(f"{url}: not a usable MHSDS data file ({e}); nothing kept")
    new_sha = h.hexdigest()
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
        print(f"downloaded {name}: {size:,} bytes, sha256 {new_sha[:16]}")
    return path


def valid_codes(cur) -> set:
    """The lad24cd codes of la_boundaries."""
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    return {r[0] for r in cur.fetchall()}


def months_between(first: str, last: str) -> list:
    """First-of-month ISO dates from first to last inclusive."""
    y, m = int(first[:4]), int(first[5:7])
    out = []
    while f"{y:04d}-{m:02d}-01" <= last:
        out.append(f"{y:04d}-{m:02d}-01")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def discover(months) -> tuple:
    """({period: [data file links naming that month]}, notes) from each
    month's publication page. A month with no page or no data file is left
    out (noted); a data file on a month's page naming another month is
    ignored (noted)."""
    links, notes = {}, []
    for p in months:
        html = fetch_page(p)
        if html is None:
            notes.append(f"{p}: no publication page")
            continue
        found = find_data_links(html)
        mine = [u for u in found if period_from_link(u) == p]
        for u in found:
            if u not in mine:
                notes.append(f"{p}: page links {_name(u)}, which names "
                             f"{period_from_link(u)}; ignored")
        if not mine:
            notes.append(f"{p}: publication page has no MHSDS data file")
            continue
        links[p] = mine
    return links, notes


# ---------------------------------------------------------------------------
# Window, cell summary and stop conditions
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


def transition(tip_file, chosen) -> tuple:
    """(kind of the tip's file or None, kind of the chosen file or None):
    ('Performance', 'Final') is the year-end refresh."""
    def kind(u):
        info = _link_info(u) if u else None
        return info[1] if info else None
    return kind(tip_file), kind(chosen)


def _total(values) -> int:
    t = 0
    for v in values:
        if v is not None:
            t += v
    return t


def cell_summary(cur, profile, by_period: dict, periods, against: str,
                 transitions=None) -> dict:
    """Per month (and in total): areas whose value differs from what the
    month is compared with (NULL equals NULL, NULL differs from 0), NULL
    replacing a number and the reverse (areas), 0 to NULL or NULL to 0,
    areas revised by more than 50%, the national totals of non-NULL values
    (held and new), the transition kind and the five largest relative
    revisions. Read-only."""
    transitions = transitions or {}
    out = {"periods_changed": 0, "areas_changed": 0, "null_for_number": 0,
           "number_for_null": 0, "zero_null": 0, "over_50pct": 0,
           "areas_one_side": 0, "new_periods": 0, "largest": [],
           "per_period": {}}
    big = []
    for p in periods:
        recs = by_period[p]
        new = {pe._key(profile, r): r["measure_value"] for r in recs}
        old_rows, _ = pe.stored(cur, profile, p, against)
        trans = transitions.get(p, (None, None))
        if not old_rows:
            out["new_periods"] += 1
            out["per_period"][p] = {
                "new": True, "transition": trans, "areas": len(new),
                "total_old": None, "total_new": _total(new.values()),
                "changed": 0, "null_areas": 0, "number_areas": 0,
                "big_areas": 0, "zero_null": 0, "one_side": 0}
            continue
        old = {k: v[0] for k, v in old_rows.items()}
        one_side = len(set(old) ^ set(new))
        changed = null_a = num_a = big_a = zero_null = 0
        for k in sorted(set(old) & set(new)):
            o, n = old[k], new[k]
            if o == n:
                continue
            changed += 1
            if n is None:
                null_a += 1
                if o == 0:
                    zero_null += 1
            elif o is None:
                num_a += 1
                if n == 0:
                    zero_null += 1
            elif o != 0:
                rel = abs(n - o) / abs(o)
                if rel > 0.5:
                    big_a += 1
                big.append((rel, p, k[0], o, n))
        t_old, t_new = _total(old.values()), _total(new.values())
        out["per_period"][p] = {
            "new": False, "transition": trans, "areas": len(new),
            "total_old": t_old, "total_new": t_new, "changed": changed,
            "null_areas": null_a, "number_areas": num_a, "big_areas": big_a,
            "zero_null": zero_null, "one_side": one_side}
        out["areas_changed"] += changed
        out["null_for_number"] += null_a
        out["number_for_null"] += num_a
        out["zero_null"] += zero_null
        out["over_50pct"] += big_a
        out["areas_one_side"] += one_side
        if changed or one_side:
            out["periods_changed"] += 1
    big.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    out["largest"] = big[:5]
    return out


def total_move(d: dict) -> "float | None":
    """The relative move of the national total, held to new (None for a new
    month; infinite when the held total is 0 and the new is not)."""
    if d.get("total_old") is None:
        return None
    o, n = d["total_old"], d["total_new"]
    if o == 0:
        return 0.0 if n == 0 else float("inf")
    return abs(n - o) / abs(o)


def stop_problems(summary: dict, transitions=None, acknowledged=()) -> dict:
    """{month: message} for held months that break a stop condition and are
    not acknowledged. Performance to Final (the year-end refresh): only the
    national total moving by more than 50% stops. Any other revision: NULL
    replacing a number in more than MAX_NULL_AREAS areas, or a value revised
    above 50% in more than MAX_BIG_REVISION_AREAS areas. Always: a 0 becoming
    NULL or the reverse (rule 1.10). --acknowledge PERIOD releases the month.
    transitions {period: (tip kind, chosen kind)} default to the summary's
    own."""
    transitions = transitions or {}
    out = {}
    for p, d in summary["per_period"].items():
        if d.get("new") or p in acknowledged:
            continue
        trans = transitions.get(p, d.get("transition", (None, None)))
        msgs = []
        if tuple(trans) == (PERFORMANCE, FINAL):
            move = total_move(d)
            if move is not None and move > MAX_FINAL_TOTAL_MOVE:
                msgs.append(f"Performance to Final: the national total moves "
                            f"{d['total_old']:,} -> {d['total_new']:,} "
                            f"({move * 100:.1f}%, limit "
                            f"{MAX_FINAL_TOTAL_MOVE * 100:.0f}%)")
        else:
            if d["null_areas"] > MAX_NULL_AREAS:
                msgs.append(f"NULL replaces a number in {d['null_areas']} "
                            f"areas (limit {MAX_NULL_AREAS})")
            if d["big_areas"] > MAX_BIG_REVISION_AREAS:
                msgs.append(f"values revised above 50% in {d['big_areas']} "
                            f"areas (limit {MAX_BIG_REVISION_AREAS})")
        if d["zero_null"]:
            msgs.append(f"{d['zero_null']} cell(s) go from 0 to NULL or NULL "
                        "to 0 (rule 1.10; read them, then --acknowledge "
                        f"{p})")
        if msgs:
            out[p] = "STOP CONDITION: " + "; ".join(msgs)
    return out


def _trans_text(trans) -> str:
    a, b = trans
    if a is None and b is None:
        return "-"
    return f"{a or '?'} to {b or '?'}"


def format_cell_summary(s: dict, against: str) -> str:
    what = "the LIVE table" if against == "live" else "the latest editions"
    lines = [f"values (compared with {what}): {s['areas_changed']} areas "
             f"changed in {s['periods_changed']} month(s); NULL replacing a "
             f"number {s['null_for_number']}; a number replacing NULL "
             f"{s['number_for_null']}; 0 to NULL or NULL to 0 "
             f"{s['zero_null']}; revised above 50% {s['over_50pct']}; areas "
             f"on one side only {s['areas_one_side']}; new months "
             f"{s['new_periods']}"]
    for p in sorted(s["per_period"]):
        d = s["per_period"][p]
        if d["new"]:
            lines.append(f"  {p}: new month, {d['areas']} areas, national "
                         f"total {d['total_new']:,}")
            continue
        move = total_move(d)
        lines.append(
            f"  {p}: {_trans_text(d['transition'])}: {d['changed']} areas "
            f"changed; number to NULL {d['null_areas']}, NULL to number "
            f"{d['number_areas']}; revised above 50% {d['big_areas']}; "
            f"national total {d['total_old']:,} -> {d['total_new']:,} "
            f"({'+' if d['total_new'] >= d['total_old'] else '-'}"
            f"{(move or 0.0) * 100:.1f}%)")
    if s["largest"]:
        lines.append("largest revisions (relative):")
        for rel, p, code, o, n in s["largest"]:
            lines.append(f"  {p} {code}: {o} -> {n} "
                         f"({'+' if n >= o else '-'}{rel * 100:.1f}%)")
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
    '9b'). Called only on `sync-new --commit` and `load --commit`."""
    pe.log_run(cur, PROFILE, rows_written, notes, started_at)


def load_run_notes(stats: dict, held: list, page: dict, acked: dict,
                   transitions: dict) -> str:
    kinds = stats["kinds"]
    done = stats["periods"]
    by = {k: [p for p in done if kinds[p] == k]
          for k in ("new", "revised", "unchanged", LIVE_MISSING)}
    stored = by["new"] + by["revised"]
    finals = [p for p in by["revised"]
              if tuple(transitions.get(p, ())) == (PERFORMANCE, FINAL)]
    head = ("Stored: " + "; ".join(f"{k} {', '.join(by[k])}"
                                   for k in ("new", "revised") if by[k])
            if stored else "Nothing new: no new month and no revision")
    if finals:
        head += (f". Performance to Final (year-end refresh): "
                 f"{', '.join(finals)}")
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
    cur.execute(f"SELECT DISTINCT lad24cd FROM public.{spec.live_table} "
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


def _read_month(period, kind, path, link_name) -> tuple:
    """(rows, meta) of one month's file with identity checked."""
    p = Path(path)
    try:
        rows, meta = parse_file(p)
    except ValueError as e:
        halt(f"{p.name}: {e}; nothing stored")
    problems = check_identity(period, kind, meta, link_name)
    if problems:
        halt(f"file identity check failed for {link_name}, nothing stored: "
             + "; ".join(problems))
    if not rows:
        halt(f"{p.name}: no {MEASURE} local authority rows; nothing stored")
    return rows, meta


def _iso_month(text: str) -> str:
    m = re.fullmatch(r"([0-9]{4})-([0-9]{2})(?:-01)?", text or "")
    if not m:
        raise argparse.ArgumentTypeError(
            f"not a first-of-month date YYYY-MM-01 (or YYYY-MM): {text!r}")
    return f"{m.group(1)}-{m.group(2)}-01"


def _this_month() -> str:
    t = date.today()
    return f"{t.year:04d}-{t.month:02d}-01"


def _offline_file(path_given: Path) -> tuple:
    """(period, kind, version, rows, meta) of a --file, its identity read
    from the file itself (one period, one status); a name that names a
    month or kind must agree."""
    try:
        rows, meta = parse_file(path_given)
    except ValueError as e:
        halt(f"--file {path_given.name}: {e}; nothing stored")
    if len(meta["periods"]) != 1 or len(meta["statuses"]) != 1:
        halt(f"--file {path_given.name}: the file carries periods "
             f"{meta['periods']} and statuses {meta['statuses']}; one of "
             "each is expected; nothing stored")
    period, kind = meta["periods"][0], meta["statuses"][0]
    if kind not in KINDS:
        halt(f"--file {path_given.name}: STATUS {kind!r} is not one of "
             f"{KINDS}; nothing stored")
    info = _link_info(path_given.name)
    version = info[2] if info else 1
    return period, kind, version, rows, meta


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

            offline = None
            if args.file:
                path_given = Path(args.file)
                offline = _offline_file(path_given)
                page = {offline[0]: path_given.name}
                print(f"file given: nothing downloaded; the file is "
                      f"{offline[0]} {offline[1]} (read from the file)")
            else:
                first = args.min_period or held[0]
                months = months_between(first, _this_month())
                print(f"publication pages: {len(months)} month(s) "
                      f"{months[0]} .. {months[-1]}")
                links, notes = discover(months)
                for n in notes:
                    print(f"  note: {n}")
                if not links:
                    halt("no MHSDS data file found on any publication page; "
                         "nothing stored")
                page = {}
                for p, urls in sorted(links.items()):
                    try:
                        page[p] = choose_link(urls)
                    except ValueError as e:
                        halt(f"{p}: {e}")
                print(f"pages: {len(page)} month(s) with a data file "
                      f"{min(page)} .. {max(page)}")
                missing_held = [p for p in held if p not in page]
                if missing_held:
                    print("NOTE: held months with no data file on their page "
                          f"(not rechecked): {', '.join(missing_held)}")
                if held and max(page) < max(held):
                    msg = (f"the pages' latest month is {max(page)[:7]} but "
                           f"{max(held)[:7]} is already held")
                    if args.allow_older_file:
                        print(f"NOTE: --allow-older-file given; {msg}")
                    else:
                        halt(msg + "; the pages look older than what is "
                             "held. If this is deliberate, re-run with "
                             "--allow-older-file")

            new_p, recheck, earlier = plan_months(
                held, page, tips, checked, recheck_all=args.recheck_all,
                recheck=args.recheck or (), min_period=args.min_period)
            if args.file:
                recheck = [p for p in page if p in held]
                new_p = [p for p in page if p not in held]
                earlier = []
            recheck = sorted(set(recheck) | {p for p in stranded
                                             if p in page})
            for p in args.recheck or ():
                if p not in held:
                    halt(f"--recheck {p}: not a held month")
                if p not in page:
                    halt(f"--recheck {p}: no data file on its page; nothing "
                         "to read")
            print(f"planned: new {', '.join(new_p) or 'none'}; recheck "
                  f"{len(recheck)} held month(s)"
                  + (f" ({recheck[0]} .. {recheck[-1]})" if recheck else "")
                  + (f"; editions months missing from live "
                     f"{', '.join(p for p in stranded if p in page)}"
                     if any(p in page for p in stranded) else ""))
            not_reread = [p for p in held if p in page and p not in recheck]
            if not_reread:
                print(f"not re-read (file unchanged or already checked): "
                      f"{len(not_reread)} month(s)"
                      + (f" ({', '.join(not_reread)})"
                         if len(not_reread) <= 6 else ""))
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
                if args.file:
                    older = file_older(offline[1], offline[2], tips.get(p))
                    offers = f"the file given is {_name(page[p])}"
                else:
                    older = link_older(page[p], tips.get(p))
                    offers = f"the page offers {_name(page[p])}"
                if older:
                    msg = (f"{p}: {offers} but the "
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
                                 offline)
        conn.rollback()  # commit mode has committed month by month already
        if not writing:
            print("PREVIEW: nothing written to the database (use --commit or "
                  "--simulate; downloads are kept in data/raw/s9b_mhsds)")
        elif args.simulate:
            print("SIMULATION: ROLLED BACK (nothing persisted)")
        return rc
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _run_months(args, conn, cur, spec, periods, page, tips, expected,
                required, against, held, ack, offline) -> int:
    """Fetch, read and check each planned month, then compare and store."""
    started = datetime.now(timezone.utc)
    infos, rows_by, metas, kinds, transitions = {}, {}, {}, {}, {}
    for p in periods:
        if offline is not None:
            path = Path(args.file)
            source_file = path.name
            kind, version = offline[1], offline[2]
            rows, meta = offline[3], offline[4]
            problems = check_identity(p, kind, meta, source_file)
            if problems:
                halt(f"file identity check failed for {source_file}, nothing "
                     "stored: " + "; ".join(problems))
            print(f"NOTE: --file: {p} is stored with source_file "
                  f"{source_file} (no link URL known)")
        else:
            url = page[p]
            kind, version = classify_link(url)
            path = fetch_month(url, RAW_DIR)
            source_file = url
            rows, meta = _read_month(p, kind, path, _name(url))
        rows_by[p], metas[p], kinds[p] = rows, meta, (kind, version)
        infos[p] = (source_file, path, content_sha256(path))
        transitions[p] = ((transition(tips.get(p), None)[0], kind)
                          if p in held else (None, kind))
        print(f"  {p}: {_name(source_file)}: {kind} v{version}; {len(rows)} "
              f"LA rows; dropped {meta['dropped'] or 'none'}; STATUS "
              f"{meta['statuses_raw']}")
    seen = {p: {r["lad24cd"] for r in rows_by[p]} for p in periods}
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
    forms = {p: sorted(c for c in seen[p] if c in geography.NEW_CODES
                       | geography.OLD_CODES) for p in periods}
    print("Barnsley/Sheffield as published: " + "; ".join(
        f"{p[:7]} {'/'.join(c[-2:] for c in cs) or '-'}"
        for p, cs in forms.items()))
    valid = valid_codes(cur)
    unknown = sorted({recode_map.get(c, c) for s in seen.values() for c in s}
                     - valid)
    if unknown:
        halt("unresolved local authority codes "
             + str([f"UNEXPLAINED {c}" for c in unknown])
             + "; explain them in la_code_lookup before loading; nothing "
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
    summary = cell_summary(cur, profile, by_period, ok, against, transitions)
    print(format_cell_summary(summary, against))
    all_stops = stop_problems(summary, transitions)
    stops = stop_problems(summary, transitions, ack)
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
        kind, version = kinds[p]
        trans = transitions[p]
        note = ("year-end Final replacing the Performance figures"
                if tuple(trans) == (PERFORMANCE, FINAL) else "")
        labels[p] = FileInfo(source_file, path, sha, release_label(
            p, kind, version, metas[p], sha, note,
            acked.get(p, "").removeprefix("STOP CONDITION: ")))
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
                    load_run_notes(stats, held, page, acked, transitions),
                    started)
            conn.commit()
            print("pipeline_run_log row written")
        else:
            print("pipeline_run_log: no row written (the run failed or "
                  "rejected a month; months committed before are in the "
                  "editions table)")
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S9b MHSDS MHS26 CRFD: editions "
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
    p = sub.add_parser("load", help="read each month's current data file, "
                       "compare and store months (preview by default)")
    p.add_argument("--file", metavar="FILE",
                   help="one local data file, zip or CSV (nothing is "
                   "downloaded; month and status are read from the file)")
    p.add_argument("--min-period", type=_iso_month, metavar="YYYY-MM-01",
                   help="look up every month's page from this month "
                   "(required when nothing is held)")
    p.add_argument("--recheck-all", action="store_true",
                   help="read every held month's file again")
    p.add_argument("--recheck", action="append", metavar="PERIOD",
                   type=_iso_month,
                   help="read this held month again (repeatable)")
    p.add_argument("--expected-areas", type=int, metavar="N",
                   help="areas per month (default the held modal count)")
    p.add_argument("--allow-older-file", action="store_true",
                   help="load a file ranking below the month's latest file "
                   "(a Performance file after a Final), or when the pages' "
                   "latest month is earlier than the held latest (default: "
                   "halt)")
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
