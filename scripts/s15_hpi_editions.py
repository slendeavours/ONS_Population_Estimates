"""S15 (Land Registry / ONS UK House Price Index): edition history and loader
on the period-editions engine.

la_house_prices holds one row per authority and month (seven price columns):
the latest layer. This module owns la_house_prices_editions, which keeps every
fetch of a month that differed from the one before, so a revision in a new
UK HPI release never overwrites what was held. The editions machinery is
editions_core driven by SPEC, through period_editions (pe) bound to PROFILE.
The pure part (file names, parsing, records, page parsers) comes first and is
ported from the retired scripts/historical/s15_hpi_build.py with identical behaviour; the loader follows.
Importing this module needs no database, no network and no key.

Periods are the live table's DATE column, handled as ISO strings
('2026-07-01'), which sort as dates (docs/RULES.md rule 8).

Blanks and zeros (docs/RULES.md rule 1): blank, 'NA' or unparseable cells
(the Land Registry's suppressed low-volume prices) are NULL, never 0; a
published 0 stays 0; NULL versus 0 counts as a revision.

Files: each run reads the collections page, follows the newest "data
downloads" page and downloads Average-prices-YYYY-MM.csv and
Average-prices-Property-Type-YYYY-MM.csv to data/raw/ (git-ignored), or
reads the two files given with --avg-prices and --property-type (offline).
Identity is read from the files: both file names name the same edition, the
edition is the latest Date in the data, and (on a download) the page names
the same month; anything else halts before anything is stored.

Run log: `sync-new --commit` and `load --commit` write one pipeline_run_log
row (agent 'Source 15 - Land Registry UK HPI', source_number and source_code
'15', status 'success') when the run succeeds, including a `load` that
rechecked and found nothing new (rows_written 0). rows_written is the edition
rows stored plus the live rows inserted (a new month counts twice: its
edition 1 and its live rows). Previews and --simulate persist nothing; a
failed or partly rejected `load` logs nothing.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back):
    python scripts/s15_hpi_editions.py ddl [--commit | --simulate]
        # create the editions table and its append-only triggers; idempotent
    python scripts/s15_hpi_editions.py status
        # what needs action, including any editions month missing from live;
        # exit 1 if anything (or if the editions table does not exist yet)
    python scripts/s15_hpi_editions.py sync-new [--expected-authorities N]
                                                [--commit | --simulate]
        # edition 1 'as loaded' for every live month with no editions
    python scripts/s15_hpi_editions.py load [--avg-prices F --property-type F]
                                            [--min-period YYYY-MM-DD]
                                            [--recheck-n N] [--expected-areas N]
                                            [--commit | --simulate]
        # the window: every held month present in the file is rechecked
        # (only the latest N with --recheck-n), and every month in the file
        # that is not held and later than the earliest held month is new;
        # months earlier than the earliest held are never loaded unless
        # --min-period widens the window (which then also sets the file's
        # first month read; default 2022-01-01). With nothing held
        # --min-period is required. Each month is checked whole before
        # anything is compared: it must hold the expected number of areas
        # (the held modal count, or --expected-areas N) and every area of
        # the live table's latest month; a month that fails is REJECTED (not
        # stored, the run exits 1) while the other months go through. Then
        # new (edition 1 AND its live rows, one transaction, checked equal),
        # unchanged (nothing stored) or revised (next edition; live changes
        # only through refresh-latest). If the editions table does not exist
        # yet the preview compares with the LIVE table and says so.
    python scripts/s15_hpi_editions.py refresh-latest [--commit | --simulate]
                                                      [--accept-drift PERIOD]
        # copy each month's latest edition into the live table, under the
        # core's before/after hash guard (PERIOD as YYYY-MM-DD)
"""
import argparse
import csv
import dataclasses
import hashlib
import html as html_lib
import math
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import load_checks  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402
from statxplore_months import plan_months  # noqa: E402

ENGLISH_LA_PREFIXES = ("E06", "E07", "E08", "E09")
MIN_PERIOD = date(2022, 1, 1)

# Barnsley and Sheffield (docs/RULES.md rule 4): the loader takes its recode
# map from geography.resolve (la_code_lookup) and stops if the files disagree
# with S15's declared form ('new'). This copy of the fallback is only the
# default for the pure build_records tests.
HARD_RECODES = dict(geography.RECODES_FALLBACK)

BASE_URL = "https://www.gov.uk"

# Column scales: prices numeric(12,2), annual change numeric(6,2).
_PRICE_Q = Decimal("0.01")
_CHANGE_Q = Decimal("0.01")
COLUMN_QUANTA = {
    "avg_price_all": _PRICE_Q,
    "avg_price_all_sa": _PRICE_Q,
    "annual_change_pct": _CHANGE_Q,
    "avg_price_detached": _PRICE_Q,
    "avg_price_semi": _PRICE_Q,
    "avg_price_terraced": _PRICE_Q,
    "avg_price_flat": _PRICE_Q,
}
VALUE_COLUMNS = tuple(COLUMN_QUANTA)

EDITION_RE = re.compile(r"^Average-prices-(?:Property-Type-)?(\d{4}-\d{2})\.csv$")


def edition_from_filename(name: str) -> str:
    """'Average-prices-2026-07.csv' or 'Average-prices-Property-Type-2026-07.csv'
    -> '2026-07'. Anything else raises ValueError naming the input."""
    m = EDITION_RE.match(name) if isinstance(name, str) else None
    if not m:
        raise ValueError(f"not a recognised HPI file name: {name!r}")
    return m.group(1)


def safe_numeric(val):
    if val is None or val == "" or val == "NA":
        return None
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except (ValueError, TypeError):
        return None


def reconcile(area_code, *, valid_lads, code_lookup, hard_recodes):
    """Hard recodes first, then the code itself if valid, then the lookup."""
    if area_code in hard_recodes:
        return hard_recodes[area_code]
    if area_code in valid_lads:
        return area_code
    if area_code in code_lookup:
        return code_lookup[area_code]
    return None


def _dec(val, quantum):
    """CSV text -> Decimal at the column scale, or None (never 0 for blanks)."""
    v = safe_numeric(val)
    if v is None:
        return None
    return Decimal(repr(v)).quantize(quantum, rounding=ROUND_HALF_UP)


def build_records(avg_rows, pt_rows, *, valid_lads, code_lookup,
                  hard_recodes=HARD_RECODES, min_period=MIN_PERIOD):
    """Join the two CSVs on (Area_Code, Date), keep English areas from
    min_period on, reconcile codes. Returns (records_by_period, unresolved,
    no_property_type_count)."""
    pt_lookup = {}
    for r in pt_rows:
        key = (r["Area_Code"], r["Date"])
        # Only rows that could be used: same filters as the average-prices file.
        if not r["Area_Code"].startswith(ENGLISH_LA_PREFIXES):
            continue
        if date.fromisoformat(r["Date"]) < min_period:
            continue
        if key in pt_lookup:
            raise ValueError("property-type file repeats (Area_Code, Date) "
                             f"{key!r}; refusing to choose a row")
        pt_lookup[key] = r
    by_period = {}
    seen = {}  # (lad24cd, iso period) -> source Area_Code
    unresolved = {}
    no_pt = 0
    for r in avg_rows:
        area_code = r["Area_Code"]
        if not area_code.startswith(ENGLISH_LA_PREFIXES):
            continue
        period = date.fromisoformat(r["Date"])
        if period < min_period:
            continue
        lad = reconcile(area_code, valid_lads=valid_lads,
                        code_lookup=code_lookup, hard_recodes=hard_recodes)
        if lad is None:
            unresolved[area_code] = unresolved.get(area_code, 0) + 1
            continue
        pt = pt_lookup.get((area_code, r["Date"]))
        if pt is None:
            no_pt += 1
        iso = period.isoformat()
        if (lad, iso) in seen:
            raise ValueError(
                f"duplicate record for lad24cd {lad} in period {iso}: source "
                f"codes {seen[(lad, iso)]} and {area_code}; refusing to "
                "choose a winner")
        seen[(lad, iso)] = area_code
        by_period.setdefault(iso, []).append({
            "lad24cd": lad,
            "period": iso,
            "avg_price_all": _dec(r.get("Average_Price"), _PRICE_Q),
            "avg_price_all_sa": _dec(r.get("Average_Price_SA"), _PRICE_Q),
            "annual_change_pct": _dec(r.get("Annual_Change"), _CHANGE_Q),
            "avg_price_detached": _dec(pt["Detached_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_semi": _dec(pt["Semi_Detached_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_terraced": _dec(pt["Terraced_Average_Price"], _PRICE_Q) if pt else None,
            "avg_price_flat": _dec(pt["Flat_Average_Price"], _PRICE_Q) if pt else None,
        })
    for recs in by_period.values():
        recs.sort(key=lambda x: x["lad24cd"])
    return by_period, unresolved, no_pt


def file_period_range(records_by_period):
    if not records_by_period:
        raise ValueError("no periods in the data")
    keys = sorted(records_by_period)
    return keys[0], keys[-1]


def check_identity(name1, name2, records_by_period):
    """Problems (empty list = fine). Raises ValueError for an unrecognisable
    file name."""
    e1, e2 = edition_from_filename(name1), edition_from_filename(name2)
    problems = []
    if e1 != e2:
        problems.append(f"file editions differ: {name1} is {e1}, {name2} is {e2}")
    if not records_by_period:
        problems.append("no periods in the data")
    else:
        latest = max(records_by_period)[:7]
        if latest != e1:
            problems.append(f"edition {e1} is not the latest period in the "
                            f"data (latest is {latest})")
    return problems


def _cell(r, col):
    v = r[col]
    if v is None:
        return "NULL"
    if isinstance(v, bool) or not isinstance(v, Decimal):
        raise ValueError(f"{col} must be a Decimal or None, got {v!r} for "
                         f"{r['lad24cd']}")
    return format(v.quantize(COLUMN_QUANTA[col], rounding=ROUND_HALF_UP), "f")


def content_sha256(records) -> str:
    """SHA-256 of one period's records sorted by lad24cd; NULL is distinct
    from 0."""
    rows = sorted(records, key=lambda r: (r["lad24cd"], r["period"]))
    lines = ["|".join([r["lad24cd"], r["period"]]
                      + [_cell(r, c) for c in VALUE_COLUMNS]) for r in rows]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


_DL_PAGE_RE = re.compile(
    r"""href=["']((?:https://www\.gov\.uk)?/government/statistical-data-sets/"""
    r"""uk-house-price-index-data-downloads-[^"'#?\s]*)["']""")
_AVG_RE = re.compile(r"""href=["']([^"']*?/Average-prices-(\d{4}-\d{2})\.csv[^"']*)["']""")
_PT_RE = re.compile(
    r"""href=["']([^"']*?/Average-prices-Property-Type-(\d{4}-\d{2})\.csv[^"']*)["']""")


def _absolute(href):
    """The href as an absolute URL, its HTML entities decoded (the real
    downloads page writes the query string with '&amp;')."""
    href = html_lib.unescape(href)
    return BASE_URL + href if href.startswith("/") else href


def find_download_page(html: str) -> str:
    m = _DL_PAGE_RE.search(html)
    if not m:
        raise ValueError("no uk-house-price-index-data-downloads link found; "
                         f"page excerpt: {html[:300]!r}")
    return _absolute(m.group(1))


def find_csv_links(html: str):
    """(average prices URL, property type URL, 'YYYY-MM'). Raises ValueError
    if either file is missing or the two editions differ."""
    a, p = _AVG_RE.search(html), _PT_RE.search(html)
    if not a or not p:
        raise ValueError("expected both Average-prices-YYYY-MM.csv and "
                         "Average-prices-Property-Type-YYYY-MM.csv links; "
                         f"found avg={bool(a)}, property type={bool(p)}; "
                         f"page excerpt: {html[:300]!r}")
    if a.group(2) != p.group(2):
        raise ValueError(f"file editions differ: average prices {a.group(2)}, "
                         f"property type {p.group(2)}")
    return _absolute(a.group(1)), _absolute(p.group(1)), a.group(2)


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

LIVE = "la_house_prices"
TABLE = "la_house_prices_editions"

# Live table (checked 2026-10-08 in information_schema): lad24cd varchar(9)
# NOT NULL, period date NOT NULL, avg_price_all, avg_price_all_sa,
# avg_price_detached, avg_price_semi, avg_price_terraced, avg_price_flat
# numeric(12,2) and annual_change_pct numeric(6,2) (all nullable), loaded_at
# timestamptz DEFAULT now(), primary key (lad24cd, period). The editions table
# matches it, with the core's lad24cd -> la_boundaries foreign key.
SPEC = core.EditionSpec(
    name="s15",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd",),
    period_col="period",
    value_cols=(("avg_price_all", "numeric(12,2)"),
                ("avg_price_all_sa", "numeric(12,2)"),
                ("annual_change_pct", "numeric(6,2)"),
                ("avg_price_detached", "numeric(12,2)"),
                ("avg_price_semi", "numeric(12,2)"),
                ("avg_price_terraced", "numeric(12,2)"),
                ("avg_price_flat", "numeric(12,2)")),
    refresh_cols=VALUE_COLUMNS,
    key_types=(("period", "date NOT NULL"),),
    as_loaded_date="latest",   # live periods carry several load dates
)

RUN_AGENT = "Source 15 - Land Registry UK HPI"
RUN_SOURCE = "15"
SOURCE_FILE = ("UK HPI data downloads: Average-prices and "
               "Average-prices-Property-Type CSVs")
LIVE_MISSING = pe.LIVE_MISSING

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw"          # git-ignored
COLLECTIONS_URL = ("https://www.gov.uk/government/collections/"
                   "uk-house-price-index-reports")
USER_AGENT = ("ucws-pipeline S15 loader (read-only download of the public "
              "UK HPI files)")
MIN_FILE_BYTES = 1024
AVG_COLUMNS = ("Date", "Area_Code", "Average_Price", "Average_Price_SA",
               "Annual_Change")
PT_COLUMNS = ("Date", "Area_Code", "Detached_Average_Price",
              "Semi_Detached_Average_Price", "Terraced_Average_Price",
              "Flat_Average_Price")
PRICE_COLUMNS = tuple(c for c in VALUE_COLUMNS if c != "annual_change_pct")

_MONTH_NAMES = ("January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December")
_EDITION_KEY = re.compile(r"([0-9]{4})-(0[1-9]|1[0-2])")
_PAGE_MONTH = re.compile(r"-([a-z]+)-([0-9]{4})/?$")
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def release_label(edition: str) -> str:
    """'2026-07' -> 'UK HPI July 2026 edition'."""
    m = _EDITION_KEY.fullmatch(edition) if isinstance(edition, str) else None
    if not m:
        raise ValueError(f"not a YYYY-MM edition: {edition!r}")
    return f"UK HPI {_MONTH_NAMES[int(m.group(2)) - 1]} {m.group(1)} edition"


def edition_from_page_url(url: str):
    """'...-data-downloads-july-2026' -> '2026-07'; None if the URL names no
    month."""
    m = _PAGE_MONTH.search(urlsplit(url).path)
    if not m or m.group(1).capitalize() not in _MONTH_NAMES:
        return None
    return f"{m.group(2)}-{_MONTH_NAMES.index(m.group(1).capitalize()) + 1:02d}"


def _no_label(fetched_on) -> str:
    raise ValueError("an S15 release label comes from the file edition; "
                     "store through run_profile(...)")


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s15_hpi_editions, name, ...) reaches the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUE_COLUMNS, run_agent=RUN_AGENT,
    run_source=RUN_SOURCE, heading="S15 UK HPI house prices",
    default_source_file=SOURCE_FILE,
    # derived at run time from the held modal count (run_profile)
    expected_areas=None,
    release_label=_no_label,
    content_sha256=_late("content_sha256"),
    savepoint="s15_period",
    example_label="(all, all_sa, change, detached, semi, terraced, flat)")


def _profile(spec) -> pe.Profile:
    return PROFILE.with_spec(spec)


def check_period(records: list, period: str, expected_areas,
                 required_areas) -> None:
    """ValueError unless the period is whole: an ISO date, every record of
    it, no area repeated, exactly expected_areas areas (None: not checked)
    and every area of required_areas (the live table's latest period)."""
    if not isinstance(period, str) or not _ISO_DAY.fullmatch(period):
        raise ValueError(f"not an ISO date period: {period!r}")
    seen = set()
    for r in records:
        if r.get("period") != period:
            raise ValueError(f"{period}: a record of period {r.get('period')!r}")
        if r["lad24cd"] in seen:
            raise ValueError(f"{period}: {r['lad24cd']} appears twice")
        seen.add(r["lad24cd"])
    n = len(seen)
    if expected_areas is not None and n != expected_areas:
        why = ("a short period is never stored" if n < expected_areas else
               "more areas than held; review them, then give "
               "--expected-areas N")
        raise ValueError(f"{period}: {n} areas, expected {expected_areas}; "
                         f"{why}")
    missing = sorted(set(required_areas) - seen)
    if missing:
        raise ValueError(f"{period}: {len(missing)} area(s) of the live "
                         f"table's latest period missing {missing[:10]}; a "
                         "period missing an area is never stored")


def period_problems(by_period: dict, periods, expected_areas,
                    required_areas) -> dict:
    """{period: message} for each period that fails check_period."""
    out = {}
    for p in periods:
        try:
            check_period(by_period[p], p, expected_areas, required_areas)
        except ValueError as e:
            out[p] = str(e)
    return out


def run_profile(spec, edition: str, expected_areas,
                required_areas=()) -> pe.Profile:
    """PROFILE for one run: the spec, the expected area count, the release
    label of the file edition and the per-period check."""
    label = release_label(edition)
    req = frozenset(required_areas)
    return dataclasses.replace(
        _profile(spec), expected_areas=expected_areas,
        release_label=lambda d: label,
        check_records=lambda r, p: check_period(r, p, expected_areas, req))


# ---------------------------------------------------------------------------
# Files: download, read, records
# ---------------------------------------------------------------------------

def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def source_file_text(avg_path, pt_path) -> str:
    """An edition's source_file: both file names and the first 16 hex
    characters of each file's sha256."""
    return "; ".join(f"{Path(p).name} (sha256 {file_sha256(p)[:16]})"
                     for p in (avg_path, pt_path))


def _excerpt(text, n=300) -> str:
    return repr(text[:n])


def fetch_latest_files(session=None, dest=None) -> tuple:
    """(average-prices path, property-type path, 'YYYY-MM'): the collections
    page -> the newest data-downloads page -> both CSVs, downloaded (read-only
    GETs with a User-Agent) to dest (default data/raw/, git-ignored). Both
    files are checked (more than MIN_FILE_BYTES, a header row with the
    columns the loader reads) before either is written, and the page's month
    must equal the files' edition. Any parse failure halts printing an
    excerpt of what was seen; nothing is guessed. session: anything with
    requests' get(url, headers=, timeout=) (default requests)."""
    if session is None:
        import requests
        session = requests
    dest = Path(dest) if dest is not None else RAW_DIR
    headers = {"User-Agent": USER_AGENT}

    def get(url):
        try:
            r = session.get(url, headers=headers, timeout=120)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 (any failure halts)
            halt(f"GET {url} failed, nothing downloaded: {e}")
        return r

    page = get(COLLECTIONS_URL).text
    try:
        dl_url = find_download_page(page)
    except ValueError as e:
        halt(f"the collections page {COLLECTIONS_URL} is not laid out as "
             f"expected, nothing downloaded: {e}")
    dl_page = get(dl_url).text
    try:
        avg_url, pt_url, edition = find_csv_links(dl_page)
    except ValueError as e:
        halt(f"the downloads page {dl_url} is not laid out as expected, "
             f"nothing downloaded: {e}")
    page_ed = edition_from_page_url(dl_url)
    if page_ed != edition:
        halt(f"file identity: the downloads page {dl_url} names "
             f"{page_ed or 'no month'} but its files are edition {edition}; "
             "nothing downloaded")
    bodies = []
    for url, cols in ((avg_url, AVG_COLUMNS), (pt_url, PT_COLUMNS)):
        name = Path(urlsplit(url).path).name
        try:
            if edition_from_filename(name) != edition:
                raise ValueError(f"{name} is not edition {edition}")
        except ValueError as e:
            halt(f"file identity: {e}; link {url}")
        body = get(url).content
        if len(body) <= MIN_FILE_BYTES:
            halt(f"{name}: {len(body)} bytes, expected more than "
                 f"{MIN_FILE_BYTES}; nothing written; saw {body[:200]!r}")
        lines = body[:8192].decode("utf-8-sig", errors="replace").splitlines()
        first = lines[0] if lines else ""
        header = next(csv.reader([first]), [])
        missing = [c for c in cols if c not in header]
        if missing:
            halt(f"{name}: the header row lacks {missing}; nothing written; "
                 f"saw {_excerpt(first)}")
        bodies.append((name, body))
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, body in bodies:
        p = dest / name
        p.write_bytes(body)
        print(f"downloaded {name}: {len(body):,} bytes, sha256 "
              f"{hashlib.sha256(body).hexdigest()[:16]} -> {p}")
        paths.append(p)
    print(f"downloads page: {dl_url} (edition {edition})")
    return paths[0], paths[1], edition


def read_csv(path, required_cols) -> list:
    """The CSV's rows as dicts; halts unless the header holds required_cols."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        missing = [c for c in required_cols if c not in header]
        if missing:
            halt(f"{Path(path).name}: the header row lacks {missing}; saw "
                 f"{_excerpt(','.join(header))}")
        return list(reader)


def reference_codes(cur) -> tuple:
    """(valid lad24cd set from la_boundaries, {old_code: new_code} from
    la_code_lookup). Only a lookup with exactly one target in la_boundaries
    is used (an ambiguous or dangling one leaves the code unresolved, which
    stops the load). Barnsley and Sheffield are resolved separately, through
    geography.resolve (load_file_records)."""
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT old_code, new_code FROM public.la_code_lookup")
    targets = {}
    for old, new in cur.fetchall():
        targets.setdefault(old, set()).add(new)
    lookup = {}
    for old, ts in targets.items():
        ok = ts & valid
        if len(ok) == 1:
            lookup[old] = next(iter(ok))
    return valid, lookup


def geography_codes(avg_rows, pt_rows, min_period=MIN_PERIOD) -> dict:
    """{ISO period: set of area codes} of the English, in-window rows of both
    files: what geography.resolve checks against S15's declared form."""
    out = {}
    for r in list(avg_rows) + list(pt_rows):
        if not r["Area_Code"].startswith(ENGLISH_LA_PREFIXES):
            continue
        d = date.fromisoformat(r["Date"])
        if d >= min_period:
            out.setdefault(d.isoformat(), set()).add(r["Area_Code"])
    return out


def load_file_records(avg_path, pt_path, conn, *,
                      min_period=MIN_PERIOD) -> tuple:
    """(records_by_period, unresolved, no_property_type_count, edition) from
    the two files: identity checked (check_identity: both names one edition,
    the edition the latest Date in the data; the average-prices file is not
    the property-type one), records built (build_records: English areas from
    min_period, codes reconciled through la_boundaries and la_code_lookup),
    a hard stop if Barnsley's or Sheffield's codes disagree with S15's form
    in geography.DATASET_FORM (the recode map comes from geography.resolve),
    and a hard stop on any unresolved code (reported UNEXPLAINED through
    load_checks.check_codes). Lists the periods present in only one of the
    files. Writes nothing."""
    a, b = Path(avg_path), Path(pt_path)
    try:
        edition = edition_from_filename(a.name)
        edition_from_filename(b.name)
    except ValueError as e:
        halt(f"file identity: {e}; nothing stored")
    if "Property-Type" in a.name or "Property-Type" not in b.name:
        halt(f"file identity: --avg-prices {a.name} and --property-type "
             f"{b.name} are not the average-prices and property-type files")
    avg_rows = read_csv(a, AVG_COLUMNS)
    pt_rows = read_csv(b, PT_COLUMNS)
    with conn.cursor() as cur:
        valid, lookup = reference_codes(cur)
        seen = geography_codes(avg_rows, pt_rows, min_period)
        try:
            recode_map, problems = geography.resolve(
                cur, RUN_SOURCE, set().union(*seen.values()) if seen else (),
                by_period=seen)
        except ValueError as e:
            halt(f"Barnsley/Sheffield geography: {e}; nothing stored")
        if problems:
            halt("Barnsley/Sheffield geography check failed: "
                 + "; ".join(problems))
        note = geography.confirm_note(RUN_SOURCE)
        if note:
            print(note)
        try:
            by_period, unresolved, no_pt = build_records(
                avg_rows, pt_rows, valid_lads=valid, code_lookup=lookup,
                hard_recodes={k: v for k, v in recode_map.items() if k != v},
                min_period=min_period)
        except ValueError as e:
            halt(f"{a.name} / {b.name}: {e}; nothing stored")
        problems = check_identity(a.name, b.name, by_period)
        if problems:
            halt("file identity check failed, nothing stored: "
                 + "; ".join(problems))
        if unresolved:
            codes = sorted(unresolved)
            found = load_checks.check_codes(cur, codes)
            found += [f"UNEXPLAINED {c}" for c in codes
                      if f"UNEXPLAINED {c}" not in found]
            halt(f"unresolved UK HPI area codes {found} (rows: "
                 f"{dict(sorted(unresolved.items()))}); explain them in "
                 "la_code_lookup before loading; nothing stored")
    pt_dates = set()
    for r in pt_rows:
        if r["Area_Code"].startswith(ENGLISH_LA_PREFIXES):
            d = date.fromisoformat(r["Date"])
            if d >= min_period:
                pt_dates.add(d.isoformat())
    avg_only = sorted(set(by_period) - pt_dates)
    pt_only = sorted(pt_dates - set(by_period))
    print(f"files: {a.name} ({len(avg_rows):,} rows), {b.name} "
          f"({len(pt_rows):,} rows); edition {edition}")
    print(f"  area-months with no property-type row: {no_pt}")
    if avg_only:
        print("  periods in the average-prices file only (stored with the "
              f"four property-type columns NULL): {', '.join(avg_only)}")
    if pt_only:
        print("  periods in the property-type file only (not loaded: no "
              f"average prices): {', '.join(pt_only)}")
    return by_period, unresolved, no_pt, edition


# ---------------------------------------------------------------------------
# Window, expected areas, cell summary
# ---------------------------------------------------------------------------

def plan_window(held: list, available: list, *, widen: bool,
                recheck_n=None) -> tuple:
    """(new, recheck, earlier), each ascending. Default: recheck every held
    period present in the file (the latest recheck_n if given), new every
    period in the file not held and later than the earliest held
    (statxplore_months.plan_months over ISO strings), earlier the file's
    periods before the earliest held (never loaded). widen (--min-period
    given): every period in the file is new or rechecked."""
    if widen:
        new = [p for p in sorted(available) if p not in held]
        recheck = [p for p in sorted(available) if p in held]
        if recheck_n is not None:
            recheck = recheck[-recheck_n:] if recheck_n > 0 else []
        return new, recheck, []
    new, recheck = plan_months(held, available,
                               recheck_n if recheck_n is not None else 0,  # not a source value
                               recheck_n is None)
    earliest = min(held) if held else None
    earlier = [p for p in sorted(available)
               if earliest is not None and p < earliest]
    return new, recheck, earlier


def held_area_count(cur, spec=SPEC):
    """The most common per-period area count of the live table (None if it
    is empty)."""
    return core.modal_count(core.authority_counts(cur, spec))


def latest_live_areas(cur, spec=SPEC) -> set:
    """The areas of the live table's latest period (empty if none)."""
    cur.execute(f"SELECT lad24cd FROM public.{spec.live_table} "
                f"WHERE {spec.period_col} = (SELECT MAX({spec.period_col}) "
                f"FROM public.{spec.live_table})")
    return {r[0] for r in cur.fetchall()}


def cell_summary(cur, profile, by_period: dict, periods, against: str) -> dict:
    """Cells that differ between each period's records and what it is
    compared with (NULL equals NULL, NULL differs from 0): per column, NULL
    replacing a number and the reverse, price revisions above 50%, the five
    largest relative price revisions (annual_change_pct is counted, not
    ranked); and the zero or negative prices in the records. Read-only."""
    cols = profile.value_cols
    out = {"periods_changed": 0, "cells": 0, "by_col": {c: 0 for c in cols},
           "null_for_number": 0, "number_for_null": 0, "over_50pct": 0,
           "areas_one_side": 0, "new_periods": 0, "new_cells": 0,
           "nonpositive": 0, "largest": []}
    big = []
    for p in periods:
        recs = by_period[p]
        out["nonpositive"] += sum(1 for r in recs for c in PRICE_COLUMNS
                                  if r[c] is not None and r[c] <= 0)
        old, _ = pe.stored(cur, profile, p, against)
        if not old:
            out["new_periods"] += 1
            out["new_cells"] += sum(1 for r in recs for c in cols
                                    if r[c] is not None)
            continue
        new = {r["lad24cd"]: tuple(r[c] for c in cols) for r in recs}
        out["areas_one_side"] += len(set(old) ^ set(new))
        changed = 0
        for k in sorted(set(old) & set(new)):
            for c, o, n in zip(cols, old[k], new[k]):
                if (o is None) == (n is None) and (o is None or o == n):
                    continue
                changed += 1
                out["by_col"][c] += 1
                if n is None:
                    out["null_for_number"] += 1
                elif o is None:
                    out["number_for_null"] += 1
                elif o != 0 and c in PRICE_COLUMNS:
                    # prices only: an annual change of 0.1 -> 1.3 is a
                    # 1200% move on a small base and says nothing
                    rel = abs(Decimal(n) - Decimal(o)) / abs(Decimal(o))
                    if rel > Decimal("0.5"):
                        out["over_50pct"] += 1
                    big.append((rel, p, k, c, o, n))
        if changed:
            out["periods_changed"] += 1
            out["cells"] += changed
    big.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    out["largest"] = big[:5]
    return out


def format_cell_summary(s: dict, against: str) -> str:
    what = "the LIVE table" if against == "live" else "the latest editions"
    cols = ", ".join(f"{c} {n}" for c, n in s["by_col"].items() if n) or "none"
    lines = [f"cells (compared with {what}): {s['cells']} changed in "
             f"{s['periods_changed']} period(s) (by column: {cols}); NULL "
             f"replacing a number {s['null_for_number']}; a number replacing "
             f"NULL {s['number_for_null']}; price revisions above 50% "
             f"{s['over_50pct']}; areas on one side only "
             f"{s['areas_one_side']}; zero or negative prices in the file "
             f"{s['nonpositive']}; new periods {s['new_periods']} "
             f"({s['new_cells']} non-NULL cells)"]
    if s["largest"]:
        lines.append("largest price revisions (relative):")
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
    """period_editions.status: editions_core.status plus 'live_missing'."""
    return pe.status(cur, _profile(spec))


def format_status(st: dict) -> str:
    return pe.format_status(PROFILE, st)


def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT,
    source_number and source_code '15', status 'success'). Called only on
    `sync-new --commit` and `load --commit`."""
    pe.log_run(cur, PROFILE, rows_written, notes, started_at)


def load_run_notes(stats: dict, edition: str, files: str, held: list,
                   available: list) -> str:
    """Words for the run log: what was checked, what was stored."""
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
    span = f"{done[0]} .. {done[-1]}" if done else "none"
    live_rows = stats.get("live_rows", 0)  # not a source value
    return (f"{head}. Checked {len(done)} month(s) ({span}); unchanged "
            f"{len(by['unchanged'])}. {release_label(edition)}; files "
            f"{files}. Held latest {max(held) if held else '-'}; file latest "
            f"{max(available) if available else '-'}. Rows stored: "
            f"{stats['stored_rows']} edition rows, {live_rows} live rows "
            "inserted. A run that finds nothing new is logged because the "
            "check is the run.")


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


def _derive_expected(cur, spec, override):
    """The area count a period must hold: --expected-areas N, else the held
    modal count; halts if neither exists."""
    derived = held_area_count(cur, spec)
    if override is not None and derived is not None and override != derived:
        print(f"NOTE: --expected-areas {override} overrides the held modal "
              f"count {derived}")
    expected = override if override is not None else derived
    if expected is None:
        halt("no live period to derive the area count from; give "
             "--expected-areas N")
    return expected


def cmd_load(args) -> int:
    spec = SPEC
    base = _profile(spec)
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            has_ed = table_exists(cur, spec.editions_table)
            if writing:
                if not has_ed:
                    halt(pe.no_table(base))
                _, new, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new:
                    halt(f"live months with no editions "
                         f"{[pe._p(x) for x in new]}; run sync-new first so "
                         "the history starts from what is held")
            against = "editions" if has_ed else "live"
            if not has_ed:
                print(f"NOTE: {spec.editions_table} does not exist yet; this "
                      "preview compares each period with the LIVE table "
                      f"{spec.live_table}")
            held = pe.held_periods(cur, base, has_ed)
            print("held periods: " + (f"{held[0]} .. {held[-1]} ({len(held)})"
                                      if held else "none"))
            # both before any download
            if not held and args.min_period is None:
                halt("nothing is held, so no window can be planned; give "
                     "--min-period YYYY-MM-DD (and --expected-areas N)")
            expected = _derive_expected(cur, spec, args.expected_areas)
            required = latest_live_areas(cur, spec)
            min_period = args.min_period or MIN_PERIOD
            if args.avg_prices:
                avg, pt = Path(args.avg_prices), Path(args.property_type)
                fetched = None
                print("files given: nothing downloaded")
            else:
                avg, pt, fetched = fetch_latest_files()
            by_period, _, _, edition = load_file_records(
                avg, pt, conn, min_period=min_period)
            if fetched is not None and fetched != edition:
                halt(f"file identity: the downloads page named edition "
                     f"{fetched}, the files are {edition}")
            available = sorted(by_period)
            print(f"{release_label(edition)}: file periods {available[0]} .. "
                  f"{available[-1]} ({len(available)} from {min_period})")
            new_p, recheck, earlier = plan_window(
                held, available, widen=args.min_period is not None,
                recheck_n=args.recheck_n)
            print(f"planned: new {', '.join(new_p) or 'none'}; recheck "
                  f"{len(recheck)} held period(s)"
                  + (f" ({recheck[0]} .. {recheck[-1]})" if recheck else ""))
            if earlier:
                print(f"ignored, earlier than the earliest held period "
                      f"{held[0]} (give --min-period to widen): "
                      f"{', '.join(earlier)}")
            periods = sorted(set(new_p) | set(recheck))
            print(f"expected areas per period: {expected}"
                  + (" (--expected-areas)" if args.expected_areas is not None
                     else " (held modal count)")
                  + "; every area of the live table's latest period "
                  f"({len(required)}) required")
            profile = run_profile(spec, edition, expected, required)
            problems = period_problems(by_period, periods, expected, required)
            for p, msg in problems.items():
                print(f"  {p}: REJECTED, not stored: {msg}")
            ok = [p for p in periods if p not in problems]
            print(format_cell_summary(
                cell_summary(cur, profile, by_period, ok, against), against))
            stats = {"periods": [], "kinds": {}, "stored_rows": 0,
                     "live_rows": 0}
            started = datetime.now(timezone.utc)
            rc = 0
            if ok:
                rc = pe.load_periods(
                    cur, profile, ok, lambda p: by_period[p], date.today(),
                    args.commit, simulate=args.simulate, against=against,
                    stats=stats, source_file=source_file_text(avg, pt))
            else:
                print("nothing to compare")
            if problems:
                print(f"REJECTED {len(problems)} period(s), not stored: "
                      f"{', '.join(problems)}; exit 1")
                rc = 1
            if args.commit:
                if rc == 0:
                    # Stored something, or rechecked and found nothing new:
                    # either way the check is the run, so it is logged.
                    log_run(cur, stats["stored_rows"]
                            + stats["live_rows"],  # not a source value
                            load_run_notes(stats, edition,
                                           source_file_text(avg, pt), held,
                                           available), started)
                    conn.commit()
                    print("pipeline_run_log row written")
                else:
                    print("pipeline_run_log: no row written (the run failed "
                          "or rejected a period; periods committed before "
                          "are in the editions table)")
        conn.rollback()  # commit mode has committed period by period already
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


def _iso_day(text: str) -> date:
    try:
        if not _ISO_DAY.fullmatch(text):
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="S15 UK HPI house prices: "
                                 "editions loader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table (preview by "
                       "default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("sync-new", help="edition 1 'as loaded' for live months "
                       "with no editions (preview by default)")
    p.add_argument("--expected-authorities", type=int, metavar="N",
                   help="authorities per month; only when nothing can be "
                   "derived (no month has editions yet)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_sync_new)
    p = sub.add_parser("load", help="read the latest files, compare and store "
                       "months (preview by default)")
    p.add_argument("--avg-prices", metavar="FILE",
                   help="a local Average-prices-YYYY-MM.csv (with "
                   "--property-type; nothing is downloaded)")
    p.add_argument("--property-type", metavar="FILE",
                   help="a local Average-prices-Property-Type-YYYY-MM.csv")
    p.add_argument("--min-period", type=_iso_day, metavar="YYYY-MM-DD",
                   help="read the file from this month and load every month "
                   "in it (widens or narrows the window; required when "
                   "nothing is held)")
    p.add_argument("--recheck-n", type=int, metavar="N",
                   help="recheck only the latest N held months (default all)")
    p.add_argument("--expected-areas", type=int, metavar="N",
                   help="areas per month (default the held modal count)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each month's latest "
                       "edition into the live table (preview by default)")
    pe.mode_parser(p)
    p.add_argument("--accept-drift", action="append", metavar="PERIOD",
                   help="overwrite this month (YYYY-MM-DD) although its live "
                   "rows equal no stored edition (repeatable)")
    p.set_defaults(func=cmd_refresh_latest)
    args = ap.parse_args(argv)
    if args.cmd == "load" and bool(args.avg_prices) != bool(args.property_type):
        ap.error("--avg-prices and --property-type are given together or not "
                 "at all")
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
