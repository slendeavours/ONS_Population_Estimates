"""S10 (MHCLG rough sleeping snapshot in England, autumn): edition history
and loader on the period-editions engine.

The live table la_rough_sleeping keeps its name, key (lad24cd,
snapshot_year) and columns and is the latest-edition layer (W1 and the map
read it at the newest snapshot_year). This module owns its editions table
and file-check ledger, append-only:

    SPEC     la_rough_sleeping_editions             period snapshot_year
    ledger   la_rough_sleeping_editions_file_checks

An edition is what one snapshot file says about one snapshot year: per
authority rough_sleeping (the file's column for that year) and
rough_sleeping_prev_year (its column for the year before). The annual
release "Rough sleeping snapshot in England: autumn <yyyy>" carries one
tables file (.ods) whose Table_1_Total restates every year from autumn 2010.
So a file states its newest year (a new period) and every held year it
covers (compared with the held edition: a back-series revision is caught).
Back-filling the years before the first held one is not done by load.

History (so the wording here stays truthful): the n8n workflow "Rough
Sleeping Snapshot (S10)" loaded the table on 2026-03-26 (pipeline_run_log
17) from a CSV converted in a chat session, whose 2025 and 2024 columns held
the autumn 2021 and 2020 snapshots. On 2026-08-19 23:59 UTC the table was
rewritten from the autumn 2025 file (scripts/verify/src/rs_autumn2025.ods)
by a load with no run-log row and no committed code; every held row carries
that loaded_at. migrate-legacy (one-off) records the held year as edition 1
"as loaded", with a proof that this parser reproduces every held value from
the held file (sha256 7b7ed536d0d51f83..., the same bytes as data/raw/
s10_rough_sleeping/Rough_sleeping_snapshot_in_England__autumn_2025.ods).

Blanks and zeros (docs/RULES.md rule 1): the file defines [x] (Not
Available), [z] (Not Applicable) and [n] (No data available as the
authority was created through reorganisation), and none of them appears in
any LA cell of any year 2010-2025 of the autumn 2025 file. So an LA cell of
a loaded year must be a non-negative whole number; a marker (named with its
meaning from the file's own shorthand line), a blank, text, a negative or a
non-integer halts (la_cell), and is never read as 0. A published 0 stays 0
(15 authorities in 2025, 12 in 2024).

Identity (rule 3), from the file itself on every path: the Cover's title
'Annual rough sleeping snapshot in England: autumn <yyyy>', its 'Autumn
2010 to autumn <yyyy>' line, its 'Publication Date' line (and the Cover's
date cell, when there is one, must be the same day) and its 'Next Release'
line; Table_1_Total's title '... 2010 - <yyyy>' (the Cover's year), the
shorthand line defining [x], [z] and [n], and the header row exactly
'Local Authority Code, Local Authority Name, Region Code, Region Name, 2010
.. <yyyy>'. The release page's year (when the page is read) and --release
must equal the file's year. Inside the file the authorities must sum to the
England row and to each region row, and 'Rest of England' must be England
less London, for every year read (reconcile). The rank of a file is its own
(publication date, year), never its name or link.

Geography (rule 4): geography.resolve('10', codes, by_period={year: codes})
(source 10 is declared 'new': an E08000016 or E08000019 halts), then each
code's canonical form must be in la_boundaries; anything else is UNEXPLAINED
and stops.

Stop conditions (calibrated on the autumn 2025 file's national series:
2020 to 2021 -9.1%, 2021 to 2022 +25.6%, 2022 to 2023 +27.0%, 2023 to 2024
+19.7%, 2024 to 2025 +2.7%; the largest move of one authority in each of
those years: Westminster -55 and Camden +55 (2021), Westminster +63 (2022),
Camden +31 (2023), Westminster +111 (2024), Exeter +40 (2025). The pandemic
year 2019 to 2020 (-37.0%, Hillingdon -95, Westminster -91) would have
stopped on the national limit and needed --acknowledge):

    new year:   national total moving more than NEW_TOTAL_PCT against the
                previous held year, or any authority moving by more than
                NEW_AREA_ABS
    held year:  any change at all against the held edition (a revision of a
                back year has never happened: the autumn 2024 and autumn
                2025 files agree on every LA cell 2010-2024), listed with
                the national total of either column moving more than
                REV_TOTAL_PCT and more than REV_MAX_AREAS authorities
                changing
    both:       fewer authorities than EXPECTED_AREAS, or (held year) an
                authority of the held edition missing: a partial file never
                replaces a fuller edition, and no flag releases that

A stopped year is REJECTED (nothing stored, no ledger row, exit 1);
--acknowledge YEAR releases every stop of that year but a partial file.
Equal rank with different content (two files claiming the same release)
stops unless --accept-reissue YEAR. Each is written to the edition's label
and the run log.

W1 input: a new year's live rows take loaded_at = now(); a revision reaches
live only through refresh-latest, which copies the edition's loaded_at, so
run refresh-latest in the same session before W1.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
year.
    python scripts/s10_rough_sleeping_editions.py ddl [--commit | --simulate]
    python scripts/s10_rough_sleeping_editions.py status
    python scripts/s10_rough_sleeping_editions.py load [--release YYYY]
        [--file PATH [--no-page]] [--recheck YYYY] [--allow-older-file]
        [--acknowledge YYYY ...] [--accept-reissue YYYY ...]
        [--commit | --simulate]
        # the file is downloaded to data/raw/s10_rough_sleeping/ (also in a
        # preview; a preview writes nothing to the database)
    python scripts/s10_rough_sleeping_editions.py refresh-latest
        [--commit | --simulate]
    python scripts/s10_rough_sleeping_editions.py migrate-legacy FILE
        [--commit | --simulate]
    python scripts/s10_rough_sleeping_editions.py restore-edition YYYY N
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
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s10_rough_sleeping_editions"
RUN_SOURCE = "10"
LIVE_MISSING = pe.LIVE_MISSING

API = "https://www.gov.uk/api/content"
GOV_UK = "https://www.gov.uk"
COLLECTION_PATH = "/government/collections/homelessness-statistics"
COLLECTION_TITLE = "Homelessness statistics"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s10_rough_sleeping"     # git-ignored
USER_AGENT = ("ucws-pipeline S10 loader (read-only download of the public "
              "MHCLG rough sleeping snapshot tables)")
MIN_FILE_BYTES = 100 * 1024
ODS_MIME = b"application/vnd.oasis.opendocument.spreadsheet"
NO_PAGE = "--file --no-page: the release page was not read"

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-10:
# lad24cd character varying NOT NULL, snapshot_year integer NOT NULL,
# rough_sleeping and rough_sleeping_prev_year integer (nullable; 0 NULL held),
# loaded_at timestamptz DEFAULT now(); PK (lad24cd, snapshot_year)). The
# editions table holds both values NOT NULL: a blank halts, never stored.
# ---------------------------------------------------------------------------

VALUE_TYPES = (("rough_sleeping", "integer NOT NULL"),
               ("rough_sleeping_prev_year", "integer NOT NULL"))
VALUES = tuple(c for c, _ in VALUE_TYPES)
KEY = "lad24cd"
PERIOD = "snapshot_year"

# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

COVER = "Cover"
TABLE = "Table_1_Total"
FIRST_YEAR = 2010
COVER_TITLE_RE = re.compile(r"Annual rough sleeping snapshot in England: "
                            r"autumn ([0-9]{4})")
RANGE_RE = re.compile(r"Autumn ([0-9]{4}) to autumn ([0-9]{4})")
PUB_RE = re.compile(r"Publication Date: ([0-9]{1,2})(?:st|nd|rd|th)? "
                    r"([A-Za-z]+) ([0-9]{4})")
NEXT_RE = re.compile(r"Next Release: (.+)")
TABLE_TITLE_RE = re.compile(r"Table 1: Estimated number of people sleeping "
                            r"rough, by local authority district and region, "
                            r"2010 - ([0-9]{4})")
SHORTHAND_RE = re.compile(r"\[([a-z])\] = ([^.\[]+?)\s*\.")
MARKER_LETTERS = ("x", "z", "n")
HEADER_IDS = ("Local Authority Code", "Local Authority Name", "Region Code",
              "Region Name")
DISTRICT_RE = re.compile(r"E0[6-9][0-9]{6}")
REGION_RE = re.compile(r"E12[0-9]{6}")
ENGLAND = "E92000001"
LONDON = "E12000007"
NA = "[z]"
REST = "Rest of England"
EXPECTED_AREAS = 296

# ---------------------------------------------------------------------------
# Stop conditions (see the module docstring for the calibration)
# ---------------------------------------------------------------------------

NEW_TOTAL_PCT = 30          # new year: national total against the year before
NEW_AREA_ABS = 150          # new year: any authority's count against it
REV_TOTAL_PCT = 1           # held year: national total of either column
REV_MAX_AREAS = 10          # held year: authorities changing
PARTIAL = "PARTIAL FILE"    # problems no --acknowledge releases

# ---------------------------------------------------------------------------
# Discovery: the GOV.UK content API
# ---------------------------------------------------------------------------

DOC_TITLE_RE = re.compile(r"Rough sleeping snapshot in England: autumn "
                          r"([0-9]{4})")
# A title that looks like the series, however it is worded ('Rough sleeping
# in England: autumn 2018', 'Annual rough sleeping snapshot ...'); the
# quarterly 'Rough sleeping data framework' titles do not.
SERIES_RE = re.compile(r"rough sleeping.*(snapshot|autumn)", re.I)
TABLES_RE = re.compile(r"Rough sleeping snapshot in England: autumn "
                       r"([0-9]{4}) - tables")
ACCESSIBLE_RE = re.compile(r"Rough sleeping snapshot in England: autumn "
                           r"([0-9]{4}) - tables \(accessible\)")
# When the next release is expected. The publisher's stated cadence is annual:
# the autumn Y snapshot is published the following winter (the autumn 2025
# Cover says 'Next Release: Winter 2026/2027' for autumn 2026), and winter ends
# with February. So the autumn Y+1 release is overdue from 1 March of Y+2,
# derived from the newest release listed, so it works every cycle.
# EXPECTED_RELEASES holds a date the publisher has announced, as data that
# overrides the derived one: {year of the release: the date the warning starts}.
EXPECTED_RELEASES = {}


def expected_next_release(newest_year, announced=None) -> tuple:
    """(year, date from which it is overdue) of the release after
    `newest_year`: the announced date if one is held for that year, else 1
    March of the year after it would be published (cadence: annual, winter
    publication)."""
    nxt = int(newest_year) + 1
    table = EXPECTED_RELEASES if announced is None else announced
    return nxt, table.get(nxt, date(nxt + 1, 3, 1))

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-10
# ---------------------------------------------------------------------------

# (rows, the survey hash: md5 of lad24cd|snapshot_year|rough_sleeping|
# rough_sleeping_prev_year ordered by snapshot_year, lad24cd; see live_state)
LEGACY_LIVE = (296, "772625863ca8f259" "6ec7633e0e303a21")
# the held file (sha256 split so the credential scan does not flag it); the
# URL is the publisher's tables attachment of the autumn 2025 release page,
# which the ledger row names so that a page load finds the file checked
LEGACY_FILE = {
    "name": "Rough_sleeping_snapshot_in_England__autumn_2025.ods",
    "sha256": "7b7ed536d0d51f837552be0207f76d0f"
              "62838ce4e72409370f6482543c710c89",
    "url": "https://assets.publishing.service.gov.uk/media/"
           "699daa5807d7bff3604d6c20/"
           "Rough_sleeping_snapshot_in_England__autumn_2025.ods",
}
# before the editions exist (a preview against live): the held rows came from
# that file, whose Cover says autumn 2025, published 26 February 2026
LEGACY_RANK = (date(2026, 2, 26), 2025)


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


def rank_text(rank) -> str:
    """'autumn 2025, published 2026-02-26' of a (date, year) rank."""
    if not rank:
        return "no file rank"
    return f"autumn {rank[1]}, published {rank[0].isoformat()}"


def _year_cell(v) -> "int | None":
    """A header year (2010, 2010.0 or '2010'); None if it is not one."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, numbers.Integral):
        return int(v)
    if isinstance(v, numbers.Real):
        if math.isnan(v) or not float(v).is_integer():
            return None
        return int(v)
    s = _norm(v)
    return int(s) if re.fullmatch(r"[0-9]{4}", s) else None


def _as_date(v) -> "date | None":
    """A date cell (datetime, pandas Timestamp or date); None otherwise."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _documents(collection_json) -> list:
    d = _json(collection_json)
    return list(((d.get("links") or {}).get("documents")) or [])


def _titles(collection_json) -> list:
    return [_norm(d.get("title")) for d in _documents(collection_json)]


def check_collection(collection_json) -> None:
    """ValueError unless the collection is still titled COLLECTION_TITLE."""
    t = _norm(_json(collection_json).get("title"))
    if t != COLLECTION_TITLE:
        raise ValueError(f"the collection is titled {t!r}, not "
                         f"{COLLECTION_TITLE!r}; the page has changed")


def release_candidates(collection_json) -> dict:
    """{year: [(title, base_path)]} of the documents titled 'Rough sleeping
    snapshot in England: autumn <yyyy>'."""
    out = {}
    for doc in _documents(collection_json):
        t = _norm(doc.get("title"))
        mm = DOC_TITLE_RE.fullmatch(t)
        if mm and doc.get("base_path"):
            out.setdefault(int(mm.group(1)), []).append((t, doc["base_path"]))
    return out


def _strays(titles, newest) -> list:
    """Titles that do not fit the strict pattern but look like the series
    and name a later year than newest, or no year."""
    out = []
    for t in titles:
        if DOC_TITLE_RE.fullmatch(t) or not SERIES_RE.search(t):
            continue
        ys = [int(y) for y in re.findall(r"(?<![0-9])([0-9]{4})(?![0-9])", t)]
        if not ys or any(y > newest for y in ys):
            out.append(t)
    return out


def latest_release(collection_json) -> tuple:
    """(year, base_path) of the newest 'Rough sleeping snapshot in England:
    autumn <yyyy>' document. ValueError (listing the titles seen) when none
    has the title, when two claim the newest year, or when a document that
    looks like the series names a later year, or none, in a title that does
    not fit (never passed over for an older release)."""
    cands = release_candidates(collection_json)
    titles = _titles(collection_json)
    if not cands:
        raise ValueError("no document titled 'Rough sleeping snapshot in "
                         "England: autumn <yyyy>' in the collection; titles "
                         f"seen: {titles[:40]}")
    newest = max(cands)
    stray = _strays(titles, newest)
    if stray:
        raise ValueError(f"the newest matching release is autumn {newest}, "
                         "but the collection lists a document that looks "
                         "like the series, with a later year or none, whose "
                         f"title does not fit: {stray}; not choosing an "
                         f"older release over it; titles seen: {titles[:40]}")
    if len(cands[newest]) != 1:
        raise ValueError(f"{len(cands[newest])} documents for autumn "
                         f"{newest}: {[t for t, _ in cands[newest]]}; refusing "
                         "to choose one")
    return newest, cands[newest][0][1]


def newest_listed(collection_json) -> "int | None":
    cands = release_candidates(collection_json)
    return max(cands) if cands else None


def release_for_year(collection_json, year) -> tuple:
    """(year, base_path) of that year's release (--release YYYY, or the
    year of a --file); ValueError unless exactly one document has the
    title."""
    cands = release_candidates(collection_json).get(int(year), [])
    if len(cands) != 1:
        raise ValueError(f"{len(cands)} documents titled 'Rough sleeping "
                         f"snapshot in England: autumn {year}' in the "
                         f"collection: {[t for t, _ in cands]}; titles seen: "
                         f"{_titles(collection_json)[:40]}")
    return int(year), cands[0][1]


def page_year(release_json) -> "int | None":
    mm = DOC_TITLE_RE.fullmatch(_norm(_json(release_json).get("title")))
    return int(mm.group(1)) if mm else None


def _attachments(page_json) -> list:
    d = _json(page_json)
    return list(((d.get("details") or {}).get("attachments")) or [])


def tables_attachment(release_json) -> dict:
    """{title, url, year, content_type, file_size, ignored} of the release
    page's one attachment titled 'Rough sleeping snapshot in England: autumn
    <yyyy> - tables' with an .ods URL. An accessible copy ('... - tables
    (accessible)') is listed in ignored and not read. ValueError listing
    every attachment title otherwise (none, several, or not .ods)."""
    atts = _attachments(release_json)
    names = [_norm(a.get("title")) for a in atts]
    hits = [a for a in atts if TABLES_RE.fullmatch(_norm(a.get("title")))]
    ignored = [n for n in names if ACCESSIBLE_RE.fullmatch(n)]
    if len(hits) != 1 or not hits[0].get("url"):
        raise ValueError(f"{len(hits)} attachments titled 'Rough sleeping "
                         "snapshot in England: autumn <yyyy> - tables'; "
                         f"attachments: {names}")
    a = hits[0]
    if Path(urlparse(a["url"]).path).suffix.lower() != ".ods":
        raise ValueError(f"the tables attachment is not an .ods file "
                         f"({a['url']}); attachments: {names}")
    title = _norm(a["title"])
    return {"title": title, "url": a["url"],
            "year": int(TABLES_RE.fullmatch(title).group(1)),
            "content_type": a.get("content_type"),
            "file_size": a.get("file_size"), "ignored": ignored}


def change_history(release_json) -> list:
    """[(public_timestamp date, note)] of the page, as published."""
    d = _json(release_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    return [(str(c.get("public_timestamp") or "")[:10], _norm(c.get("note")))
            for c in ch]


def _today() -> date:
    return date.today()


def discovery_note(collection_json, held_periods, today=None) -> list:
    """Lines to print when discovery found nothing newer than what is held:
    the newest release the collection lists, that none newer is listed, and
    a WARNING when the next release's expected date (EXPECTED_RELEASES) has
    passed. Empty when the newest listed year is not held."""
    nl = newest_listed(collection_json)
    if nl is None or str(nl) not in {str(p) for p in held_periods}:
        return []
    out = [f"the newest release the collection lists is autumn {nl}, which "
           "is already held; no newer release is listed in the collection"]
    today = today or _today()
    y, when = expected_next_release(nl)
    if today >= when:
        out.append(f"WARNING: the autumn {y} release was expected by "
                   f"{when:%d %B %Y} (the publisher's annual cadence, "
                   "winter publication, or its announced date) and "
                   f"today is {today:%d %B %Y}, but the collection does "
                   "not list it. It may be late, or discovery may be "
                   "missing it (check the GOV.UK collection page by hand; "
                   "--file PATH loads a downloaded file)")
    return out


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def la_cell(v, where, markers=None) -> int:
    """A published count: a non-negative whole number (an int, or a float
    equal to one). Anything else raises ValueError naming `where`: a marker
    such as [x] (with its meaning from the file's shorthand line, markers
    {letter: meaning}), a blank, any other text, a boolean, a negative or a
    non-integer. Never returns a default."""
    if isinstance(v, str):
        s = v.strip()
        if not s:
            raise ValueError(f"{where}: a blank cell (a blank halts and is "
                             "never read as 0)")
        mm = re.fullmatch(r"\[([a-z])\]", s)
        if mm:
            meaning = (markers or {}).get(mm.group(1))
            raise ValueError(
                f"{where}: the marker {s}"
                + (f" ({meaning!r} in the file's shorthand line)" if meaning
                   else " (not defined in the file's shorthand line)")
                + "; no marker has appeared in an LA cell before (autumn "
                "2010 to 2025), so the load halts; it is never read as 0")
        raise ValueError(f"{where}: text {v!r} is not a count")
    if v is None:
        raise ValueError(f"{where}: a blank cell (a blank halts and is never "
                         "read as 0)")
    if isinstance(v, bool):
        raise ValueError(f"{where}: {v!r} is not a count")
    if isinstance(v, numbers.Integral):
        n = int(v)
    elif isinstance(v, numbers.Real):
        if math.isnan(v):
            raise ValueError(f"{where}: a blank cell (NaN); never read as 0")
        if not float(v).is_integer():
            raise ValueError(f"{where}: non-integer {v!r}")
        n = int(v)
    else:
        raise ValueError(f"{where}: {v!r} ({type(v).__name__}) is not a count")
    if n < 0:
        raise ValueError(f"{where}: negative count {n}")
    return n


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------

def _frames(p: Path) -> dict:
    """{sheet: [row lists]} of the Cover (when present) and Table_1_Total,
    and '_sheets': every sheet name. ValueError when the file does not open
    or has no Table_1_Total."""
    import pandas as pd
    try:
        book = pd.ExcelFile(p, engine="odf")
    except Exception as e:  # noqa: BLE001 (not an .ods file)
        raise ValueError(f"{p.name}: not a readable .ods file ({e})") from None
    try:
        names = list(book.sheet_names)
        if TABLE not in names:
            raise ValueError(f"{p.name}: no {TABLE} sheet; sheets {names}")
        want = [s for s in (COVER, TABLE) if s in names]
        out = {"_sheets": names}
        for s in want:
            df = book.parse(s, header=None)
            out[s] = [list(r) for r in df.itertuples(index=False, name=None)]
    finally:
        book.close()
    return out


def _cells(rows) -> list:
    return [v for r in rows for v in r if not _blank(v)]


def _one(texts, rx, what, sheet):
    hits = [mm for mm in (rx.fullmatch(t) for t in texts) if mm]
    if len(hits) != 1:
        raise ValueError(f"{sheet}: {len(hits)} cells like {what!r}, expected "
                         f"one; cells seen: {texts[:24]}")
    return hits[0]


def _published(mm, where) -> date:
    try:
        return datetime.strptime(f"{mm.group(1)} {mm.group(2)} "
                                 f"{mm.group(3)}", "%d %B %Y").date()
    except ValueError:
        raise ValueError(f"{where}: {mm.group(0)!r} is not a date") from None


def parse_cover(rows, name="") -> dict:
    """{title, year, first_year, published, pub_text, next_release} of the
    Cover rows. ValueError (naming what was seen) unless there is exactly
    one title, range, Publication Date and Next Release line, the range
    starts in autumn FIRST_YEAR and ends in the title's year, and every date
    cell on the Cover is the publication date."""
    cells = _cells(rows)
    texts = [_norm(c) for c in cells if isinstance(c, str)]
    where = f"{name}: {COVER}"
    t = _one(texts, COVER_TITLE_RE, "Annual rough sleeping snapshot in "
             "England: autumn <yyyy>", where)
    r = _one(texts, RANGE_RE, "Autumn 2010 to autumn <yyyy>", where)
    pub = _one(texts, PUB_RE, "Publication Date: <d> <Month> <yyyy>", where)
    nxt = _one(texts, NEXT_RE, "Next Release: <when>", where)
    year, first, last = int(t.group(1)), int(r.group(1)), int(r.group(2))
    if first != FIRST_YEAR:
        raise ValueError(f"{where}: the range says autumn {first} to autumn "
                         f"{last}; the series starts in autumn {FIRST_YEAR}")
    if last != year:
        raise ValueError(f"{where}: the title says autumn {year}, the range "
                         f"ends in autumn {last}")
    published = _published(pub, where)
    for c in cells:
        d = _as_date(c)
        if d is not None and d != published:
            raise ValueError(f"{where}: a date cell says {d.isoformat()}, the "
                             f"Publication Date line says "
                             f"{published.isoformat()}")
    return {"title": t.group(0), "year": year, "first_year": first,
            "published": published, "pub_text": pub.group(0),
            "next_release": _norm(nxt.group(1))}


def _header_row(rows, name) -> int:
    for i, r in enumerate(rows[:12]):
        if len(r) > 4 and _year_cell(r[4]) == FIRST_YEAR:
            return i
    raise ValueError(f"{name}: {TABLE} has no header row with {FIRST_YEAR} "
                     f"in its fifth column in the first 12 rows; cells seen: "
                     f"{[_norm(c) for c in _cells(rows[:8])][:24]}")


def parse_table(rows, year, name="", cover_note="") -> dict:
    """The Table_1_Total facts for a Cover year: {table_title, markers,
    header, years, england, rest, regions {code: {row, name, values}}, las
    {code: {row, name, region_code, region_name, values}}, empty_rows}. values:
    {year: raw cell}. ValueError on a title that is not 'Table 1: ... 2010 -
    <year>', a shorthand line not defining [x], [z] and [n], a header that is
    not exactly HEADER_IDS then 2010..year (unknown and missing listed), a
    row that is not England, a region, Rest of England or an E06-E09
    authority, a repeated row, or data after an empty row."""
    where = f"{name}: {TABLE}"
    h = _header_row(rows, where)
    header = list(rows[h])
    while header and _blank(header[-1]):
        header.pop()
    got = [_year_cell(v) if i >= 4 else _norm(v) for i, v in enumerate(header)]
    got = [g if g is not None else _norm(header[i]) for i, g in enumerate(got)]
    want = list(HEADER_IDS) + list(range(FIRST_YEAR, year + 1))
    if got != want:
        unknown = [g for g in got if g not in want]
        missing = [w for w in want if w not in got]
        raise ValueError(f"{where}: the header row {got} is not the expected "
                         f"one: unknown {unknown}, missing {missing} (expected "
                         f"exactly {list(HEADER_IDS)} then {FIRST_YEAR} .. "
                         f"{year}){cover_note}; the reading must be corrected "
                         "deliberately, with the evidence")
    above = [_norm(c) for c in _cells(rows[:h]) if isinstance(c, str)]
    tt = _one(above, TABLE_TITLE_RE, "Table 1: Estimated number of people "
              "sleeping rough, by local authority district and region, 2010 - "
              "<yyyy>", where)
    if int(tt.group(1)) != year:
        raise ValueError(f"{where}: the title says 2010 - {tt.group(1)}, the "
                         f"Cover says autumn {year}")
    short = [t for t in above if "shorthand" in t.lower()]
    markers = {}
    for t in short:
        for letter, meaning in SHORTHAND_RE.findall(t):
            markers[letter] = _norm(meaning)
    lack = [f"[{x}]" for x in MARKER_LETTERS if x not in markers]
    if len(short) != 1 or lack:
        raise ValueError(f"{where}: {len(short)} shorthand line(s); the "
                         f"markers {lack or 'all'} are not defined there "
                         f"(expected one line defining [x], [z] and [n]); "
                         f"cells seen: {above[:8]}")
    years = list(range(FIRST_YEAR, year + 1))
    width = len(want)
    england = rest = None
    regions, las = {}, {}
    empty = 0  # trailing empty rows seen; not a source value
    for n, r in enumerate(rows[h + 1:], start=h + 2):
        if all(_blank(v) for v in r):
            empty += 1
            continue
        rw = f"{where} row {n}"
        if empty:
            raise ValueError(f"{rw}: data after {empty} empty row(s); empty "
                             "rows are allowed only at the end")
        if any(not _blank(v) for v in r[width:]):
            raise ValueError(f"{rw}: a cell beyond the {width} headed columns")
        ids = [_norm(v) for v in (list(r[:4]) + [None] * 4)[:4]]
        vals = dict(zip(years, (list(r[4:width]) + [None] * width)))
        c0, c1, c2, c3 = ids
        if DISTRICT_RE.fullmatch(c0):
            if not c1 or not REGION_RE.fullmatch(c2) or not c3:
                raise ValueError(f"{rw}: authority {c0} without a name, an "
                                 f"E12 region code or a region name ({ids})")
            if c0 in las:
                raise ValueError(f"{rw}: {c0} appears twice (also row "
                                 f"{las[c0]['row']})")
            las[c0] = {"row": n, "name": c1, "region_code": c2,
                       "region_name": c3, "values": vals}
        elif c0 == NA and c1 == NA and c2 == ENGLAND and c3 == "England":
            if england is not None:
                raise ValueError(f"{rw}: a second England row")
            england = {"row": n, "values": vals}
        elif c0 == NA and c1 == NA and c2 == NA and c3 == REST:
            if rest is not None:
                raise ValueError(f"{rw}: a second {REST} row")
            rest = {"row": n, "values": vals}
        elif c0 == NA and c1 == NA and REGION_RE.fullmatch(c2) and c3:
            if c2 in regions:
                raise ValueError(f"{rw}: region {c2} appears twice")
            regions[c2] = {"row": n, "name": c3, "values": vals}
        else:
            raise ValueError(f"{rw}: unexpected row {ids} (known: E06-E09 "
                             f"authorities, England, the E12 regions, "
                             f"{REST})")
    if england is None or rest is None or not regions or not las:
        raise ValueError(f"{where}: England row {england is not None}, "
                         f"{REST} row {rest is not None}, {len(regions)} "
                         f"region rows, {len(las)} authorities; all are "
                         "expected")
    return {"table_title": tt.group(0), "markers": markers, "header": got,
            "years": years, "england": england, "rest": rest,
            "regions": regions, "las": las, "empty_rows": empty}


def read_file(path) -> dict:
    """The facts of one snapshot tables file: parse_cover's (title, year,
    first_year, published, pub_text, next_release), parse_table's, plus path,
    sheets and rank (published, year). The Table_1_Total header is checked
    before the Cover is required, so the autumn 2024 layout (no Cover sheet,
    shifted headers) halts on its header, naming both. Raises ValueError
    naming what was seen."""
    p = Path(path)
    fr = _frames(p)
    sheets = fr["_sheets"]
    note = "" if COVER in sheets else (
        f"; the file also has no {COVER} sheet (sheets {sheets}): the autumn "
        "2024 layout, whose column headed 'Local authority' holds the codes")
    rows = fr[TABLE]
    if COVER not in sheets:
        # the year is not known without the Cover: read the header against
        # the title's year so the message names the header the file has
        h = _header_row(rows, f"{p.name}: {TABLE}")
        yrs = [_year_cell(v) for v in rows[h][4:]]
        last = max([y for y in yrs if y is not None] or [FIRST_YEAR])
        parse_table(rows, last, p.name, note)
        raise ValueError(f"{p.name}: no {COVER} sheet; sheets {sheets}")
    cover = parse_cover(fr[COVER], p.name)
    out = parse_table(rows, cover["year"], p.name, note)
    out.update(cover)
    out.update({"path": p, "sheets": sheets,
                "rank": (cover["published"], cover["year"])})
    return out


# ---------------------------------------------------------------------------
# Reconciliation inside the file
# ---------------------------------------------------------------------------

def reconcile(table, years=None) -> list:
    """Problems (empty = fine), exact, for each year read (years; default
    every year of the file): every authority and aggregate cell is a count
    (la_cell); the authorities sum to the England row and, by their region
    code, to each region row (an authority whose region has no row is a
    problem); Rest of England equals England less London (England when the
    file has no London row)."""
    out = []
    mk = table.get("markers")
    for y in (years if years is not None else table["years"]):
        if y not in table["years"]:
            out.append(f"{y}: not a year of the file")
            continue
        try:
            la = {c: la_cell(r["values"][y], f"{TABLE} row {r['row']} {c} {y}",
                             mk) for c, r in table["las"].items()}
            eng = la_cell(table["england"]["values"][y],
                          f"{TABLE} England {y}", mk)
            reg = {c: la_cell(r["values"][y], f"{TABLE} region {c} {y}", mk)
                   for c, r in table["regions"].items()}
            rest = la_cell(table["rest"]["values"][y], f"{TABLE} {REST} {y}",
                           mk)
        except ValueError as e:
            out.append(f"{y}: {e}")
            continue
        total = sum(la.values())
        if total != eng:
            out.append(f"{y}: the authorities sum to {total:,}, the England "
                       f"row says {eng:,}")
        by = {}
        for c, r in table["las"].items():
            by.setdefault(r["region_code"], []).append(la[c])
        for rc in sorted(set(by) | set(reg)):
            if rc not in reg:
                out.append(f"{y}: authorities in region {rc} but no row for "
                           "it")
                continue
            s = sum(by.get(rc, ()))
            if s != reg[rc]:
                out.append(f"{y}: the authorities of {rc} "
                           f"({table['regions'][rc]['name']}) sum to {s:,}, "
                           f"its row says {reg[rc]:,}")
        lon = reg.get(LONDON)
        want = eng - lon if lon is not None else eng
        if rest != want:
            out.append(f"{y}: {REST} says {rest:,}, England less London is "
                       f"{want:,}")
    return out


def england_total(table, year) -> int:
    return la_cell(table["england"]["values"][year], f"{TABLE} England {year}",
                   table.get("markers"))


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

def _getter(resolve):
    return resolve if callable(resolve) else (lambda c: resolve[c])


def build_records(table, year, resolve, provenance=None) -> list:
    """The snapshot year's records from a read file, one per authority,
    sorted by lad24cd: {lad24cd, snapshot_year (the year as a string, the
    engine's period), rough_sleeping (the file's column for the year),
    rough_sleeping_prev_year (its column for the year before)}. resolve:
    {publisher code: lad24cd} or a callable. provenance: optional {name:
    value} added to every record for the caller (S10's tables have no
    provenance columns, so nothing of it is stored or compared). ValueError
    when the file cannot state the year (it or the year before missing), a
    cell is not a count (la_cell, the marker's meaning from the file), a
    code resolves to nothing, or a lad24cd repeats."""
    y = int(year)
    if y not in table["years"] or y - 1 not in table["years"]:
        raise ValueError(f"the file covers {table['years'][0]}-"
                         f"{table['years'][-1]}; it cannot state {y} (needs "
                         f"{y - 1} and {y})")
    get = _getter(resolve)
    mk = table.get("markers")
    out, seen = [], {}
    for code, r in table["las"].items():
        where = f"{TABLE} row {r['row']} {code}"
        rs = la_cell(r["values"][y], f"{where} {y}", mk)
        prev = la_cell(r["values"][y - 1], f"{where} {y - 1}", mk)
        lad = get(code)
        if not lad:
            raise ValueError(f"{where}: {code} resolves to no lad24cd")
        if lad in seen:
            raise ValueError(f"{where}: duplicate key {lad} (also {seen[lad]}); "
                             "refusing to choose a winner")
        seen[lad] = code
        rec = {KEY: lad, PERIOD: str(y), "rough_sleeping": rs,
               "rough_sleeping_prev_year": prev}
        if provenance:
            rec.update(provenance)
        out.append(rec)
    out.sort(key=lambda x: x[KEY])
    return out


def periods_stated(table, held_years) -> list:
    """The years a file states: its newest year, plus every held year it
    covers (a year y with y and y-1 in the file). Sorted ints."""
    years = set(table["years"])
    out = {table["years"][-1]}
    for h in held_years:
        y = int(h)
        if y in years and y - 1 in years:
            out.add(y)
    return sorted(out)


def rows_content_sha(records) -> str:
    """sha256 of a year's content: one line per record sorted by lad24cd,
    lad24cd|rough_sleeping|rough_sleeping_prev_year, joined by LF, UTF-8.
    Nothing else is in it, so where a file was read never makes a new
    edition."""
    lines = sorted(f"{r[KEY]}|{r['rough_sleeping']}|"
                   f"{r['rough_sleeping_prev_year']}" for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def resolve_codes(cur, codes, period) -> tuple:
    """({publisher code: lad24cd}, problems). geography.resolve(cur, '10',
    codes, by_period={period: codes}) (source 10 is declared 'new': an old
    Barnsley or Sheffield code is a problem); then each code's canonical
    form must be in la_boundaries, else 'UNEXPLAINED'. Two codes reaching
    one lad24cd are a problem."""
    codes = set(codes)
    recode, problems = geography.resolve(cur, RUN_SOURCE, codes,
                                         by_period={period: codes})
    problems = list(problems)
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    valid = {r[0] for r in cur.fetchall()}
    out, hit = {}, {}
    for c in sorted(codes):
        r = geography.canonical(c, recode)
        if r in valid:
            out[c] = r
            hit.setdefault(r, []).append(c)
        else:
            problems.append(f"UNEXPLAINED {c}: not in la_boundaries and not a "
                            "la_code_lookup recode")
    two = {k: v for k, v in hit.items() if len(v) > 1}
    if two:
        problems.append(f"{period}: codes {two} reach one lad24cd; refusing "
                        "to choose a winner")
    return out, problems


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_RANK_RE = re.compile(r"\bautumn ([0-9]{4}); published ([0-9]{4}-[0-9]{2}-"
                      r"[0-9]{2})\b")
_LEDGER_RE = re.compile(r"(.*) \(autumn ([0-9]{4}); published ([0-9]{4}-"
                        r"[0-9]{2}-[0-9]{2}); periods ([0-9]{4}(?:, "
                        r"[0-9]{4})*)\)")


def release_rank(file_or_source) -> "tuple | None":
    """(publication date, year) of a file: a read file's own rank, or the
    'autumn <yyyy>; published <yyyy-mm-dd>' an edition's or ledger row's
    source_file carries; None if it names none (a file name, URL or media
    id is never read for a rank)."""
    x = file_or_source
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    if not mm:
        return None
    return (date.fromisoformat(mm.group(2)), int(mm.group(1)))


def edition_source(file, src, note="") -> str:
    """An edition's source_file: the file name, its own year and publication
    date (the rank), where it was read, and a note."""
    return (f"{src['file_name']}; autumn {file['year']}; published "
            f"{file['published'].isoformat()}; {src['where']}"
            + (f"; {note}" if note else ""))


def ledger_source(where, rank, periods) -> str:
    """A file's ledger source_file: where it was read (the final URL, or the
    path of a --file), its own year and publication date, and the periods it
    states, so a later run knows them without reading the file."""
    ps = ", ".join(str(p) for p in sorted(int(p) for p in periods))
    return (f"{where} (autumn {rank[1]}; published {rank[0].isoformat()}; "
            f"periods {ps})")


def parse_ledger_source(s) -> "tuple | None":
    """(where, rank, [periods as strings]) of a ledger source_file, or
    None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    rank = (date.fromisoformat(mm.group(3)), int(mm.group(2)))
    return mm.group(1), rank, mm.group(4).split(", ")


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held period: the newest file rank that stated it}: the tip
    edition's, or a later file the ledger records for the period (a file
    found unchanged stores no edition)."""
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, allow_older=False,
                    stranded=()) -> "list | None":
    """The periods the ledger records this file's (where, sha256) pair for,
    if that covers every period the file needs (its ledger source declares
    them); else None (the file is read and each period planned). A held
    period whose tip comes from a newer file is not needed (skipped as
    older) unless allow_older; a stranded period (editions, no live rows)
    always makes the file be read. So a run that failed or was rejected
    part-way leaves a period without its ledger row and the rerun reads the
    file again."""
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
    """(new, revised, skipped). periods: the years the file states, as
    strings; rank: its own; ranks: {held period: tip_ranks}; checked:
    {period: {(source, sha)}} of the ledger (stranded periods left out by
    the caller, so they are always read). new: not held; revised: held,
    compared with the tip; skipped {period: reason}: held periods whose tip
    comes from a newer file, and new periods older than the newest held
    year (back-filling: not done by load), unless allow_older (the
    older-file guard, on every path); held periods whose (source, sha) the
    ledger already records, unless --recheck names it. --recheck must be a
    held period of the file (ValueError)."""
    periods = sorted(str(p) for p in periods)
    if recheck is not None and (recheck not in periods or recheck not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held year this file "
                         f"states ({', '.join(periods)})")
    newest_held = max(ranks) if ranks else None
    new, revised, skipped = [], [], {}
    for p in periods:
        if p not in ranks:
            if newest_held is not None and p < newest_held and not allow_older:
                skipped[p] = (f"older: autumn {p} is before the newest held "
                              f"year {newest_held}; back-filling is not done "
                              "by load")
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
# Stop conditions
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


def _total(records, col) -> int:
    return sum(r[col] for r in records or ())


def period_problems(new, tip, prev, *, kind) -> list:
    """The stop conditions of one year (empty = fine). new: the file's
    records; tip: (kind 'revised') the held edition's records of the same
    year; prev: (kind 'new') the latest earlier held year's records, or
    None. Problems starting with PARTIAL are released by nothing: fewer
    authorities than EXPECTED_AREAS (both kinds), or an authority of the
    held edition missing (revised). New: the national rough_sleeping moving
    by more than NEW_TOTAL_PCT against prev, or any authority's by more than
    NEW_AREA_ABS. Revised: any change at all (a back-year revision has never
    happened), with the national total of either column moving by more than
    REV_TOTAL_PCT and more than REV_MAX_AREAS authorities changing listed as
    their own problems. All but PARTIAL are released by --acknowledge."""
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
        a, b = _total(prev, "rough_sleeping"), _total(new, "rough_sleeping")
        if _over(a, b, g["NEW_TOTAL_PCT"]):
            out.append(f"national rough_sleeping {a:,} -> {b:,} against the "
                       f"previous held year (limit {g['NEW_TOTAL_PCT']}%)")
        pk = _by_key(prev)
        big = [f"{k} {pk[k]['rough_sleeping']:,}->{nk[k]['rough_sleeping']:,}"
               for k in sorted(set(pk) & set(nk))
               if abs(nk[k]["rough_sleeping"] - pk[k]["rough_sleeping"])
               > g["NEW_AREA_ABS"]]
        if big:
            out.append(f"rough_sleeping moves by more than {g['NEW_AREA_ABS']} "
                       f"against the previous held year in {len(big)} "
                       f"authorit(ies): {', '.join(big[:6])}")
        return out
    tk = _by_key(tip)
    gone = sorted(set(tk) - set(nk))
    if gone:
        out.append(f"{PARTIAL}: {len(gone)} authorit(ies) of the held edition "
                   f"missing from the file (a partial file never replaces a "
                   f"fuller edition): {', '.join(gone[:8])}")
    changed = [k for k in sorted(set(tk) | set(nk))
               if k not in tk or k not in nk
               or any(tk[k][c] != nk[k][c] for c in VALUES)]
    if changed:
        ex = [f"{k} {tuple(tk[k][c] for c in VALUES) if k in tk else 'absent'}"
              f" -> {tuple(nk[k][c] for c in VALUES) if k in nk else 'absent'}"
              for k in changed[:4]]
        out.append(f"{len(changed)} authorit(ies) change against the held "
                   f"edition, e.g. {', '.join(ex)}; a revision of a back year "
                   "has never happened in this series (the autumn 2024 and "
                   "autumn 2025 files agree on every LA cell 2010-2024)")
    for c in VALUES:
        a, b = _total(tip, c), _total(new, c)
        if _over(a, b, g["REV_TOTAL_PCT"]):
            out.append(f"national {c} {a:,} -> {b:,} (limit "
                       f"{g['REV_TOTAL_PCT']}%)")
    if len(changed) > g["REV_MAX_AREAS"]:
        out.append(f"{len(changed)} authorities change against the held "
                   f"edition (limit {g['REV_MAX_AREAS']})")
    return out


def _differs(new, old) -> bool:
    nk, ok = _by_key(new), _by_key(old)
    if set(nk) != set(ok):
        return True
    return any(nk[k][c] != ok[k][c] for k in nk for c in VALUES)


# ---------------------------------------------------------------------------
# Spec and profile
# ---------------------------------------------------------------------------

SPEC = core.EditionSpec(
    name="s10",
    live_table="la_rough_sleeping",
    editions_table="la_rough_sleeping_editions",
    key_cols=(KEY,),
    period_col=PERIOD,
    value_cols=VALUE_TYPES,
    refresh_cols=VALUES + ("loaded_at",),
    refresh_from=(("loaded_at", "loaded_at"),),
    key_types=((KEY, "varchar(9) NOT NULL"), (PERIOD, "integer NOT NULL")),
    fk_la_boundaries=True,
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S10 release label comes from the file; store "
                     "through apply_year(...)")


def check_records(records, period) -> None:
    """ValueError unless the year's records are whole: each of this year,
    one per lad24cd, both values present."""
    seen = set()
    for r in records:
        if r.get(PERIOD) != period:
            raise ValueError(f"{period}: a record of year {r.get(PERIOD)!r}")
        if r[KEY] in seen:
            raise ValueError(f"{period}: {r[KEY]} appears twice")
        if any(r.get(c) is None for c in VALUES):
            raise ValueError(f"{period}: {r[KEY]} lacks a value")
        seen.add(r[KEY])


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUES, run_agent=RUN_AGENT, run_source=RUN_SOURCE,
    heading="S10 MHCLG rough sleeping snapshot (la_rough_sleeping)",
    default_source_file="MHCLG rough sleeping snapshot tables",
    expected_areas=EXPECTED_AREAS, release_label=_no_label,
    content_sha256=rows_content_sha, check_records=check_records,
    file_checks=True, savepoint="s10_period",
    example_label="(rough_sleeping, rough_sleeping_prev_year)")


def profile() -> pe.Profile:
    """PROFILE bound to the current SPEC and EXPECTED_AREAS (looked up when
    called, so tests can swap them)."""
    g = globals()
    return dataclasses.replace(PROFILE, spec=g["SPEC"],
                               expected_areas=g["EXPECTED_AREAS"])


def _late(name: str):
    """A call through this module's attribute `name`, looked up when called
    (so mock.patch.object(s10_rough_sleeping_editions, name, ...) reaches
    the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: the editions table, its append-only triggers and the file-check
    ledger. Idempotent; no value is written; the live table is not
    touched."""
    prof = profile()
    core.create_schema(cur, prof.spec)
    pe.create_file_checks(cur, prof)


@dataclass(frozen=True)
class PeriodInfo:
    """What a year's edition records: source_file, the release label and the
    file read ((ledger source, sha256),)."""
    source_file: str
    label: str
    files: tuple


def apply_year(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for one year, then its ledger row, inside the
    caller's per-period savepoint (s10_period): a new year's edition 1, live
    rows and ledger row commit or roll back together."""
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
                           source_file=info.source_file)
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


def _opens(path) -> None:
    """Raise unless the file is an OpenDocument spreadsheet with a
    Table_1_Total sheet."""
    with zipfile.ZipFile(path) as z:
        if z.read("mimetype").strip() != ODS_MIME:
            raise ValueError("not an OpenDocument spreadsheet")
        if f'table:name="{TABLE}"'.encode() not in z.read("content.xml"):
            raise ValueError(f"no {TABLE} sheet")


def fetch(url, dest, session=None) -> tuple:
    """Download url into the directory dest (redirects followed) and return
    (path to read, final URL). The file name is the final URL's. Checked
    before it is kept: more than MIN_FILE_BYTES, an .ods name, and it opens
    as an OpenDocument spreadsheet with a Table_1_Total sheet. A same-named
    file with the same content is kept as it is; one with different content
    is never replaced: the download is saved beside it as
    <stem>-<sha8><suffix> and that is the one read. An existing name is
    never a reason to skip the download."""
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
    if suffix != ".ods":
        halt(f"{final}: not an .ods file; nothing written")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    tmp = dest / (Path(name).stem + ".download.tmp" + suffix)
    tmp.write_bytes(body)
    try:
        _opens(tmp)
    except Exception as e:  # noqa: BLE001
        tmp.unlink()
        halt(f"{final}: does not open as the snapshot tables ({e}); nothing "
             "kept")
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


def records(cur, spec, period, edition=None) -> list:
    """The year's rows as records (lad24cd, the year as a string, both
    values): the live table's (edition None) or the given edition's."""
    names = (KEY,) + VALUES
    if edition is None:
        cur.execute(f"SELECT {', '.join(names)} FROM public.{spec.live_table} "
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
    """{period (string): {edition, source_file, rank}} of every period with
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
    rank of the held file (LEGACY_RANK)."""
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    return {pe._p(p): {"edition": None, "source_file": None,
                       "rank": LEGACY_RANK} for (p,) in cur.fetchall()}


def ledger_checks(cur, prof) -> dict:
    """{period (string): {(source_file, file_sha256)}} of the ledger."""
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
    '10'). Called only on committed load, migrate-legacy and restore-edition
    runs, partial runs included."""
    pe.log_run(cur, profile(), rows_written, notes, started_at)


def cmd_ddl(args) -> int:
    return pe.run_ddl(profile(), args, connect=_late("_conn"),
                      table_exists=_late("table_exists"),
                      create_schema=lambda cur: create_all(cur))


def status_lines(cur, prof) -> list:
    """Per period: the tip edition, its file's rank and the ledger's latest
    file."""
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
                      "--commit` (S10 has no sync-new)")
                return 1
            st = pe.status(cur, prof)
            lines = status_lines(cur, prof)
    finally:
        conn.rollback()
        conn.close()
    print(pe.format_status(prof, st, lines))
    return 0 if st["ok"] else 1


def _tables_exist(cur) -> bool:
    """True when the editions table exists; halts (S10's own wording: there
    is no sync-new) when it or the ledger does not."""
    prof = profile()
    for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
        if not table_exists(cur, t):
            halt(f"{t} does not exist yet; run `ddl --commit`, then "
                 "`migrate-legacy FILE --commit`")
    return True


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift YYYY] [--commit | --simulate]: the
    engine's refresh (each year's latest edition copied into live, loaded_at
    included, under the before/after hash guard)."""
    return pe.run_refresh_latest(
        profile(), args, connect=_late("_conn"),
        table_exists=lambda cur, t: _tables_exist(cur))


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


def _read(path) -> dict:
    try:
        return read_file(path)
    except ValueError as e:      # the message names the file already
        halt(f"{e}; nothing stored")


def identity_problems(file, page_year=None, release=None) -> list:
    """Problems (empty = fine) between a read file and where it came from:
    the release page's year (when the page was read) and --release must
    both equal the file's own year."""
    out = []
    if page_year is not None and int(page_year) != file["year"]:
        out.append(f"the release page is autumn {page_year}, the file says "
                   f"autumn {file['year']}")
    if release is not None and int(release) != file["year"]:
        out.append(f"--release {release}, the file says autumn "
                   f"{file['year']}")
    return out


def _print_page(page, base):
    print(f"release: {page.get('title')} ({GOV_UK}{base}); first published "
          f"{str(page.get('first_published_at'))[:10]}; updated "
          f"{str(page.get('public_updated_at'))[:10]}")
    for ts, note in change_history(page)[:4]:
        print(f"  change_history {ts}: {note[:200]}")


def _collection():
    coll = fetch_json(COLLECTION_PATH)
    try:
        check_collection(coll)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    return coll


def _page(base, year):
    page = fetch_json(base)
    py = page_year(page)
    if py != int(year):
        halt(f"{GOV_UK}{base}: the page title {page.get('title')!r} is not "
             f"the autumn {year} release")
    _print_page(page, base)
    return page


def _source(args) -> dict:
    """The file the run reads: {path, where, how, page_year, page_url,
    file_name, file, discovered}. --file reads a local file (nothing
    downloaded; it is read here, file set) and finds its own year's release
    page in the collection unless --no-page; otherwise the content API finds
    the release (the newest, or --release YYYY) and its tables file is
    downloaded to RAW_DIR (also in a preview)."""
    out = {"path": None, "where": None, "how": None, "page_year": None,
           "page_url": None, "file_name": None, "file": None,
           "discovered": None}
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"file given: {path}; nothing downloaded; identity is read "
              "from the file")
        f = _read(path)
        bad = identity_problems(f, None, args.release)
        if bad:
            halt("file identity check failed, nothing stored: "
                 + "; ".join(bad))
        out.update(path=path, where=_file_source(path), how="local file",
                   file_name=path.name, file=f)
        if args.no_page:
            out.update(page_url=NO_PAGE, how="local file, --no-page: the "
                       "release page was not read")
            print("NOTE: --no-page: the release page is not read")
            return out
        coll = _collection()
        try:
            y, base = release_for_year(coll, f["year"])
        except ValueError as e:
            halt(f"{GOV_UK}{COLLECTION_PATH}: {e}; the release page of the "
                 f"file's own year (autumn {f['year']}) was not found. Give "
                 "--no-page to load the file without it (recorded as such)")
        _page(base, y)
        out.update(page_year=y, page_url=GOV_UK + base)
        return out
    coll = _collection()
    try:
        if args.release:
            y, base = release_for_year(coll, args.release)
            how = f"release autumn {y} given"
        else:
            y, base = latest_release(coll)
            how = "newest release (content API)"
            out["discovered"] = coll
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    page = _page(base, y)
    try:
        att = tables_attachment(page)
    except ValueError as e:
        halt(f"{GOV_UK}{base}: {e}")
    if att["year"] != y:
        halt(f"the attachment {att['title']!r} is for autumn {att['year']}, "
             f"the release is autumn {y}")
    print(f"  attachment: {att['title']!r} {att['url']}")
    for t in att["ignored"]:
        print(f"  not read: {t!r} (an accessible copy)")
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    path, final = fetch(_abs_url(att["url"]), RAW_DIR)
    print(f"  final URL: {final}")
    out.update(path=path, where=final, how=how, page_year=y,
               page_url=GOV_UK + base, file_name=Path(urlparse(final).path).name)
    return out


def _label(file, src, sha, how, *, notes=()) -> str:
    return (f"MHCLG rough sleeping snapshot: {file['title']}; published "
            f"{file['published'].isoformat()}; {src['where']} sha256 "
            f"{sha[:16]}; {how}" + "".join(f"; {n}" for n in notes if n))


def _report(file) -> None:
    print(f"{file['path'].name}: {file['title']!r}; {file['pub_text']!r}; "
          f"next release {file['next_release']!r}; {file['table_title']!r}; "
          f"{len(file['las'])} authorities, {len(file['regions'])} region "
          f"rows; markers defined: "
          + ", ".join(f"[{k}] {v!r}" for k, v in sorted(file["markers"].items())))


def _reconciled(file, years) -> None:
    problems = reconcile(file, years)
    if problems:
        halt("reconciliation inside the file failed, nothing stored: "
             + "; ".join(problems[:8]))
    print("reconciled: the authorities sum to England and to each region "
          "row, and Rest of England is England less London, for "
          + ", ".join(f"{y} (England {england_total(file, y):,})"
                      for y in years))


def _build(cur, file, periods) -> dict:
    """{period string: records}, codes resolved; halts on a geography or
    cell problem."""
    try:
        rmap, problems = resolve_codes(cur, set(file["las"]), file["year"])
    except ValueError as e:
        halt(f"geography: {e}; nothing stored")
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    recoded = sorted((c, r) for c, r in rmap.items() if r != c)
    if recoded:
        print("codes resolved (geography.resolve, la_code_lookup recode): "
              + ", ".join(f"{c} -> {r}" for c, r in recoded))
    try:
        return {str(p): build_records(file, p, rmap) for p in periods}
    except ValueError as e:
        halt(f"{e}; nothing stored")


def _years_read(periods) -> list:
    return sorted({y for p in periods for y in (int(p) - 1, int(p))})


def cmd_load(args) -> int:
    if args.no_page and not args.file:
        halt("--no-page is only for --file (a local file whose release page "
             "is not read)")
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
            if src["discovered"] is not None:
                for line in discovery_note(src["discovered"], held):
                    print(line)
            path = src["path"]
            sha = content_sha256(path)
            print(f"{path.name}: sha256 {sha[:16]}"
                  + ("; byte-identical to the held file"
                     if sha == LEGACY_FILE["sha256"] else ""))
            f = src["file"]
            if has_ed and args.recheck is None:
                done = ledger_complete(checked, src["where"], sha, ranks,
                                       allow_older=args.allow_older_file,
                                       stranded=stranded)
                if done:
                    print(f"the file's (source, sha256) is already in the "
                          f"ledger for {', '.join(done)}; "
                          + ("nothing compared" if f else "nothing parsed")
                          + " (--recheck YYYY reads it again)")
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            if f is None:
                f = _read(path)
                bad = identity_problems(f, src["page_year"], args.release)
                if bad:
                    halt("file identity check failed, nothing stored: "
                         + "; ".join(bad))
            _report(f)
            periods = periods_stated(f, held)
            _reconciled(f, _years_read(periods))
            by_period = _build(cur, f, periods)
            lsrc = ledger_source(src["where"], f["rank"], periods)
            chk = {p: s for p, s in checked.items() if p not in stranded}
            try:
                new, revised, skipped = plan_periods(
                    list(by_period), f["rank"], ranks, chk, lsrc, sha,
                    recheck=args.recheck, allow_older=args.allow_older_file)
            except ValueError as e:
                halt(str(e))
            rc = _run_periods(args, conn, cur, prof, has_ed, src, f, by_period,
                              new, revised, skipped, tips, ranks, held, sha,
                              lsrc)
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


def _run_periods(args, conn, cur, prof, has_ed, src, f, by_period, new,
                 revised, skipped, tips, ranks, held, sha, lsrc) -> int:
    started = datetime.now(timezone.utc)
    spec = prof.spec
    rank = f["rank"]
    older_note = {}
    for p in revised + new:
        t = ranks.get(p)
        if t is not None and t > rank:
            older_note[p] = (f"--allow-older-file given; {p}'s tip comes from "
                             f"a file of {rank_text(t)}, this file is "
                             f"{rank_text(rank)}")
        elif p in new and held and p < max(held):
            older_note[p] = (f"--allow-older-file given; autumn {p} is before "
                             f"the newest held year {max(held)}")
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
        halt(f"--acknowledge/--accept-reissue {stray}: not years this run "
             f"compares or stores ({periods or 'none'}); nothing stored")
    if not periods:
        if skipped and all(r.startswith("older") for r in skipped.values()):
            halt(f"older file: every year the file states is skipped; the "
                 f"file is {rank_text(rank)}, older than what is held for "
                 f"{', '.join(sorted(skipped))}; storing it would record an "
                 "older file. If this is deliberate, re-run with "
                 "--allow-older-file")
        print("nothing to do")
        return 0
    problems, infos, acked, reissued = {}, {}, {}, {}
    for p in periods:
        recs = by_period[p]
        if p in revised:
            kind = "revised"
            tip = _tip_records(cur, spec, p, tips, has_ed)
            found = period_problems(recs, tip, None, kind=kind)
        else:
            kind = "new"
            tip = None
            earlier = [h for h in held if h < p]
            prev = (_tip_records(cur, spec, max(earlier), tips, has_ed)
                    if earlier else None)
            found = period_problems(recs, None, prev, kind=kind)
        hard = [x for x in found if x.startswith(PARTIAL)]
        soft = [x for x in found if not x.startswith(PARTIAL)]
        bad = list(hard)
        if soft and p in ack:
            acked[p] = "; ".join(soft)
            print(f"  {p}: ACKNOWLEDGED (--acknowledge): {acked[p]}")
        elif soft:
            bad += [x + f"; read the preview, then --acknowledge {p}"
                    for x in soft]
        if kind == "revised" and ranks.get(p) == rank and _differs(recs, tip):
            if p in reissue:
                reissued[p] = (f"--accept-reissue {p}: the file is "
                               f"{rank_text(rank)}, the same as the file of "
                               "the held tip, with different content (a "
                               "publisher reissue keeping its date)")
                print(f"  {p}: ACCEPTED REISSUE: {reissued[p]}")
            else:
                bad.append(f"the file is {rank_text(rank)}, the same as the "
                           "file of the held tip, but its content differs: "
                           "two files claim the same release. A publisher "
                           "reissue keeps its date: read the page's change "
                           f"note, then --accept-reissue {p}")
        if bad:
            problems[p] = bad
            continue
        notes = (older_note.get(p, ""), reissued.get(p, ""),
                 f"ACKNOWLEDGED {acked[p]}" if p in acked else "")
        how = (("new year" if kind == "new" else "restated year")
               + f" ({src['how']})")
        infos[p] = PeriodInfo(
            edition_source(f, src, "; ".join(n for n in notes if n)),
            _label(f, src, sha, how, notes=notes), ((lsrc, sha),))
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
                             f["published"], args.commit,
                             simulate=args.simulate, against="editions",
                             stats=stats, apply=apply)
    elif ok:
        rc = pe.load_periods(cur, prof, ok, lambda per: by_period[per],
                             f["published"], False, against="live")
        print("  (no editions table yet: preview against live only)")
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
        notes = (partial + f"snapshot file {src['where']} sha256 {sha[:16]}, "
                 f"{rank_text(rank)} ({src['how']}): "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(skipped)}" if skipped else "")
                 + "".join(f"; {older_note[p]}" for p in sorted(older_note)
                           if p in stats["periods"])
                 + "".join(f"; {reissued[p]}" for p in sorted(reissued)
                           if p in stats["periods"])
                 + "".join(f"; ACKNOWLEDGED {p}: {acked[p]}"
                           for p in sorted(acked) if p in stats["periods"])
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
    table. The survey hash is the spec's: md5 of lad24cd|snapshot_year|
    rough_sleeping|rough_sleeping_prev_year (NULL as '~'), rows ordered by
    snapshot_year, lad24cd, joined by LF."""
    spec = spec or globals()["SPEC"]
    cur.execute(f"""SELECT COUNT(*), md5(string_agg(coalesce(lad24cd::text,
        '~')||'|'||coalesce(snapshot_year::text,'~')||'|'||
        coalesce(rough_sleeping::text,'~')||'|'||
        coalesce(rough_sleeping_prev_year::text,'~'), E'\\n' ORDER BY
        snapshot_year, lad24cd)), COUNT(DISTINCT loaded_at),
        COUNT(DISTINCT snapshot_year) FROM public.{spec.live_table}""")
    return tuple(cur.fetchone())


def _proof(held, built) -> tuple:
    """(values compared, differences) of the held rows against the rebuilt
    rows on lad24cd and both values."""
    hk, bk = _by_key(held), _by_key(built)
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk))]
    diffs += [("only rebuilt", k) for k in sorted(set(bk) - set(hk))]
    cells = 0  # values compared; not a source value
    for k in sorted(set(hk) & set(bk)):
        for c in VALUES:
            cells += 1
            if hk[k][c] != bk[k][c]:
                diffs.append((k, c, hk[k][c], bk[k][c]))
    return cells, diffs


def migrate_legacy(cur, file, *, write, legacy=None, files=None) -> dict:
    """One-off. Preconditions: on write the editions table and ledger exist
    and are empty (in a preview they may be absent; if present they must be
    empty); live as surveyed (legacy: (rows, survey hash), default
    LEGACY_LIVE; one loaded_at; one year); the file's sha256 as
    files['sha256'] (default LEGACY_FILE). Proof, any failure halts: the
    file's identity and in-file reconciliation (the year and the year
    before); the live year is the file's newest; this parser on the file
    reproduces every live value (lad24cd and both values, Barnsley and
    Sheffield through the recode). Edition 1 'as loaded' from live (release
    label the engine's as-loaded label, published_date the load date,
    source_file 'as loaded: <name>; autumn <yyyy>; published <date>'), and a
    ledger row for the file (outcome 'unchanged', edition 1, files['url'])
    so the next page load finds it checked. Live untouched. Nothing commits
    here; the caller rolls everything back on a halt."""
    g = globals()
    legacy = tuple(legacy or g["LEGACY_LIVE"])
    files = files or g["LEGACY_FILE"]
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
    n, h, k, nper = live_state(cur, spec)
    if (n, h) != legacy or k != 1 or nper != 1:
        halt(f"{spec.live_table} is not as surveyed: {n} rows, hash {h}, {k} "
             f"loaded_at value(s), {nper} year(s); expected {legacy[0]} rows, "
             f"hash {legacy[1]}, one loaded_at, one year; nothing stored")
    path = Path(file)
    sha = content_sha256(path)
    if sha != files["sha256"]:
        halt(f"{path.name}: sha256 {sha[:16]}, expected "
             f"{files['sha256'][:16]} (the held file); nothing stored")
    f = _read(path)
    _report(f)
    period = str(f["year"])
    _reconciled(f, _years_read([period]))
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    live_periods = [pe._p(r[0]) for r in cur.fetchall()]
    if live_periods != [period]:
        halt(f"live years {live_periods}, the file's newest year is {period}; "
             "nothing stored")
    held = records(cur, spec, period)
    built = _build(cur, f, [period])[period]
    cells, diffs = _proof(held, built)
    if diffs:
        halt(f"proof failed: {len(diffs)} differences between the held rows "
             f"and the held file read again, e.g. {diffs[:5]}; nothing "
             "stored")
    cur.execute(f"SELECT MIN((loaded_at AT TIME ZONE 'UTC')::date) FROM "
                f"public.{spec.live_table}")
    loaded = cur.fetchone()[0]
    plan = {"period": period, "rows": len(held), "cells": cells,
            "records": held, "sha": sha, "loaded": loaded,
            "content_sha": rows_content_sha(held),
            "source_file": (f"as loaded: {files['name']}; autumn {f['year']}; "
                            f"published {f['published'].isoformat()}"),
            "ledger": ledger_source(files["url"], f["rank"], [period])}
    print(f"proof: the held file read again reproduces every held value: "
          f"{len(held):,} authorities matched by lad24cd, {cells:,} values "
          "(rough_sleeping, rough_sleeping_prev_year), 0 differences")
    print(f"  {period}: edition 1 as loaded, {len(held):,} rows, published "
          f"{loaded} (the load date); source_file {plan['source_file']!r}; "
          f"content sha256 {plan['content_sha']}; ledger {plan['ledger']}")
    if not write:
        return plan
    cur.execute(f"SAVEPOINT {prof.savepoint}")
    ed = core.insert_edition(
        cur, spec, held, period, release_label=core.AS_LOADED_LABEL,
        published_date=loaded, source_file=plan["source_file"],
        source_sha256=plan["content_sha"], supersedes=None, strict=True)
    if ed != 1 or core.rows_differing(cur, spec, period, ed):
        halt(f"{spec.editions_table} {period}: edition 1 does not equal "
             "live; rolled back")
    pe.record_file_check(cur, prof, period, plan["ledger"], sha,
                         "unchanged", 1)
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
                        f"loaded for {plan['period']} ({plan['rows']} rows) "
                        f"from the live table; proof {plan['cells']} values "
                        "equal the held file read again "
                        f"(sha256 {plan['sha'][:16]}); ledger row for the "
                        "file. Live untouched.", started)
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
        description="S10 MHCLG rough sleeping snapshot: editions loader on "
        "the period-editions engine. There is no sync-new; migrate-legacy "
        "records the held year once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table, its triggers "
                       "and the file-check ledger (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="find the newest release's tables file, "
                       "compare and store the years it states (preview by "
                       "default)")
    p.add_argument("--release", type=_year_arg, metavar="YYYY",
                   help="that autumn's release page (default the newest)")
    p.add_argument("--file", metavar="PATH",
                   help="a local tables file (nothing downloaded; identity "
                   "is read from the file)")
    p.add_argument("--no-page", action="store_true",
                   help="with --file only: do not read the release page "
                   "(recorded as such)")
    p.add_argument("--recheck", type=_year_arg, metavar="YYYY",
                   help="compare this held year again although the file is "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a year whose tip comes from a "
                   "newer file, or a year before the newest held (logged)")
    p.add_argument("--acknowledge", action="append", type=_year_arg,
                   metavar="YYYY",
                   help="release a year's stop conditions after reading the "
                   "preview (repeatable; logged; never a partial file)")
    p.add_argument("--accept-reissue", action="append", type=_year_arg,
                   metavar="YYYY",
                   help="store a year whose file has the held file's rank but "
                   "different content (a publisher reissue keeping its date; "
                   "repeatable; logged)")
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
                       "for the held year, with the proof (preview by "
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
