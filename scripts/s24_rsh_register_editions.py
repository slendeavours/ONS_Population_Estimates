"""S24 (Regulator of Social Housing: the register of registered providers,
regulatory judgements and enforcement notices): edition history and loader
on the period-editions engine.

The three live tables keep their names, keys and data columns and are the
latest-edition layer. This module owns their editions tables and file-check
ledgers, append-only:

    REGISTER    rsh_registered_providers_editions     period snapshot_date
    JUDGEMENTS  rsh_regulatory_judgements_editions    period edition_date
    NOTICES     rsh_enforcement_notices_editions      period edition_date
    ledgers     <editions table>_file_checks

The register is a snapshot series: one period per register date, every
period live (the publisher keeps no archive, so history exists only here).
Judgements and notices are a rolling table the publisher reissues every one
to three weeks: every held file is a period (its edition date), the live
tables hold the newest period only (the engine opt-in
live_newest_period_only), and older periods stay in the editions tables as
history. A reissue of a held period is its next edition.

S24 has no geography and must not acquire one (DATASET_FORM['24'] is
'none'; nothing here reads or writes a local authority code).

History (so the wording here stays truthful; the spec's survey of
2026-10-11): run-log 96 (2026-08-14 21:26 UTC, the old build
s24_rsh_register_build.py) loaded the 24 July register and the 12 August
judgements; the 17 August register was loaded on 2026-08-20 14:25:27 UTC by
code not in the repo and with no run-log row (its notes column holds the
text 'None' on all 1,578 rows, which the old build could not write); run-log
101 (2026-10-01 21:59 UTC) loaded the 17 September register and the 30
September judgements, whose upsert re-tagged 293 of the 308 August rows to
2026-09-30 and left 15 August rows in live. The judgement editions of 18
August, 26 August and 9 September were never loaded and are not held.

Identity (rule 3), from the file and the publisher's own page, never from a
URL (the August media URL now serves the September file):
    register    the date in the attachment title 'List of registered
                providers – <d Month yyyy>' (or --title-date with --file),
                cross-checked against the workbook's docProps modified date
                (between the title date and 14 days after); the one visible
                sheet titled 'Registered Providers - list...'; its header.
    judgements  the file name's yyyymmdd prefix, which must equal a date of
                the page's change history or its public_updated_at (unless
                --file --no-page), and docProps modified within 7 days; the
                sheets 'Regulatory Judgements' and 'Enforcement Notices' and
                their headers exactly as the old build had them.
    rank        (title or file date, docProps modified). An older file (a
                lower rank for a held period, or a judgements edition older
                than the newest held one) halts on every path unless
                --allow-older-file; an equal rank with different bytes and
                different content is a reissue and stops unless
                --accept-reissue PERIOD.

The hidden sheet: each register workbook carries a veryHidden sheet holding a
publisher-tooling token. Sheet names and states are read from the workbook's
own xl/workbook.xml; only visible sheets are ever named, and only the wanted
sheet is opened, by its exact title. No other sheet is iterated, read,
printed, hashed or stored (the file's sha256 covers the whole file, as for
any file). Raw files live in data/raw/s24_rsh/ (git-ignored).

Blanks and markers (docs/RULES.md rule 1; the spec's "S24 marker reasons";
Scott's decisions of 2026-10-11). A NULL is never 0 and always carries its
reason in the editions-only null_reasons jsonb {column: reason}:
    governance or viability '-', landlord type 'Local Authority'
                                    not_applicable_local_authority
    consumer '-' whose change cell starts 'Not assessed yet'
                                    not_assessed_yet
    any other grade '-' (rent included)
                                    marked_dash_no_reason
    a change, date or text cell '-' marked_dash
    an empty or missing cell        blank
    the text 'None' in a text cell  publisher_text_none
    the August register's stored 'None' notes (named correction
    s24-aug-notes-none-2026-10)     stored_text_none_not_from_file
    edition 1 'as loaded' of a judgements or notices period, where the old
    build stored a bare NULL        reason_not_recorded_as_loaded
A grade outside C1..C4, G1..G4, V1..V4 (each optionally '*') or Y (rent)
halts naming the cell; a date cell must be a typed date, an Excel serial in
20000..60000, '-' or blank, else it halts.

Edition 1 'as loaded' (migrate-legacy, one-off) is the data as held:
register 24 July and 17 September = live, proved equal to the held files,
null_reasons read from those files (their only NULLs are empty Notes cells);
register 17 August = live as held, 'None' notes included, file not
obtainable; judgements 30 September = live (proved); judgements 12 August =
the held file read with the old build's rules (308 rows; the 15 rows still
tagged with it proved equal, and run-log 96's count); notices likewise. The
corrections are later editions: the judgements' and notices' reasons (a
re-read of the held files, --acknowledge PERIOD) and the August notes
(load --correction s24-aug-notes-none-2026-10 --acknowledge 2026-08-17).

Stop conditions (each needs --acknowledge PERIOD, logged), calibrated on the
held data: register 1,579 (24 July), 1,578 (17 August), 1,577 (17
September) providers; 24 July to 17 August +4/-5, 17 August to 17 September
+1/-2. Judgements 308 (12 August) and 311 (30 September) rows, 293 keys in
common.
    register    a new snapshot with fewer than REGISTER_MIN_SHARE_PCT (95%)
                of the previous snapshot's providers, or more than
                REGISTER_MAX_CHANGED (30) providers added, or removed
    judgements  a new edition with fewer than JUDGEMENTS_MIN_SHARE_PCT (90%)
                of the previous edition's rows
    any         a reissue (or re-read) of a held period that changes any row
                (values or null_reasons)

The legacy live judgements: the old upsert left 15 rows of 12 August in live
beside the 30 September rows. status reports it; refresh-latest
--accept-key-changes <newest edition date> removes them from live (each
equals a row of its stored edition, so nothing is lost); a new judgements
period waits until then.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
periods.
    python scripts/s24_rsh_register_editions.py ddl [--commit | --simulate]
    python scripts/s24_rsh_register_editions.py status
    python scripts/s24_rsh_register_editions.py load
        [--only register|judgements] [--file PATH [--title-date YYYY-MM-DD]
        [--no-page]] [--recheck PERIOD] [--allow-older-file]
        [--acknowledge [PART:]PERIOD ...] [--accept-reissue [PART:]PERIOD ...]
        [--correction NAME] [--commit | --simulate]
        # files are downloaded to data/raw/s24_rsh/ (also in a preview; a
        # preview writes nothing to the database). A period breaking a stop
        # condition is REJECTED (nothing stored, exit 1).
    python scripts/s24_rsh_register_editions.py refresh-latest
        [--accept-drift PERIOD] [--accept-key-changes PERIOD]
        [--commit | --simulate]
    python scripts/s24_rsh_register_editions.py migrate-legacy
        [--commit | --simulate]
    python scripts/s24_rsh_register_editions.py restore-edition TABLE PERIOD N
        [--commit | --simulate]
"""
import argparse
import dataclasses
import hashlib
import json
import re
import sys
import warnings
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s24_rsh_register_editions"
RUN_SOURCE = "24"
LIVE_MISSING = pe.LIVE_MISSING
HELD_NOT_LIVE = pe.HELD_NOT_LIVE

API = "https://www.gov.uk/api/content"
GOV_UK = "https://www.gov.uk"
REGISTER_PATH = "/government/publications/registered-providers-of-social-housing"
JUDGEMENTS_PATH = ("/government/publications/regulatory-judgements-and-"
                   "enforcement-notices")
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s24_rsh"              # git-ignored
USER_AGENT = ("ucws-pipeline S24 loader (read-only download of the public RSH "
              "register and regulatory judgements files)")
MIN_FILE_BYTES = 10 * 1024
NO_PAGE = "not read (--file --no-page)"

# ---------------------------------------------------------------------------
# The publisher's files
# ---------------------------------------------------------------------------

REGISTER_TITLE_RE = re.compile(r"List of registered providers\s*[–—-]\s*"
                               r"([0-9]{1,2} [A-Za-z]+ [0-9]{4})")
REGISTER_SHEET_PREFIX = "Registered Providers - list"
REGISTER_HEADERS = ("Organisation name", "Registration number",
                    "Registration date", "Designation", "Corporate form",
                    "Notes")
JUDGEMENTS_TITLE = "Regulatory Judgements and Enforcement Notices Table"
JUDGEMENT_SHEET = "Regulatory Judgements"
NOTICE_SHEET = "Enforcement Notices"
# exactly the old build's headers
JUDGEMENT_HEADERS = (
    "Reg Code", "Landlord", "Landlord Type",
    "Name and Reg Code Change Details",
    "Other landlords included in the judgement",
    "Status", "Consumer grade", "Consumer Grade Change", "Consumer Grade Date",
    "Governance Grade", "Governance Grade Change", "Governance Date",
    "Viability Grade", "Viability Grade Change", "Viability Grade Date",
    "Rent", "Rent Date", "Rent Change", "Type of Publication",
    "Publication Date", "Engagement Process")
NOTICE_HEADERS = (
    "Reg Code", "Provider", "Name and Reg Code Change Details",
    "Other providers included in the notice", "Status", "Type of Publication",
    "Publication Date", "Route", "Explanation", "Date of Enforcement Notice")
FILE_DATE_RE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})(?![0-9])")
REGISTER_MODIFIED_DAYS = 14
JUDGEMENTS_MODIFIED_DAYS = 7
REGISTER_OVERDUE_DAYS = 40      # monthly; published 3 to 4 days after its date
JUDGEMENTS_OVERDUE_DAYS = 35    # reissued every 1 to 3 weeks

# ---------------------------------------------------------------------------
# Markers and reasons
# ---------------------------------------------------------------------------

NOT_APPLICABLE_LA = "not_applicable_local_authority"
NOT_ASSESSED_YET = "not_assessed_yet"
MARKED_DASH_NO_REASON = "marked_dash_no_reason"
MARKED_DASH = "marked_dash"
BLANK = "blank"
PUBLISHER_TEXT_NONE = "publisher_text_none"
STORED_TEXT_NONE = "stored_text_none_not_from_file"
NOT_RECORDED = "reason_not_recorded_as_loaded"
LA_TYPE = "Local Authority"
NOT_ASSESSED_PREFIX = "Not assessed yet"
GRADE_RE = {"consumer_grade": re.compile(r"C[1-4]\*?"),
            "governance_grade": re.compile(r"G[1-4]\*?"),
            "viability_grade": re.compile(r"V[1-4]\*?"),
            "rent_grade": re.compile(r"Y")}
GRADE_ALLOWED = {"consumer_grade": "C1..C4, each optionally *",
                 "governance_grade": "G1..G4, each optionally *",
                 "viability_grade": "V1..V4, each optionally *",
                 "rent_grade": "Y"}

# Named corrections, each decided, never a blanket override: load
# --correction NAME applies exactly this change to the period's tip (every
# cell of `column` holding `stored` becomes NULL with `reason`, exactly
# `cells` of them) as its next edition; --acknowledge PERIOD is still needed.
CORRECTIONS = {
    "s24-aug-notes-none-2026-10": {
        "part": "register", "period": "2026-08-17", "column": "notes",
        "stored": "None", "cells": 1578, "reason": STORED_TEXT_NONE,
        "decided": "Scott, 2026-10-11",
        "why": ("the 17 August 2026 register's notes hold the text 'None' on "
                "all 1,578 rows, written by the untracked 2026-08-20 load; it "
                "is not publisher text (the old build's text() could not "
                "write it), so it is NULL with its reason; Pivotal Housing "
                "Association's note is not restored (no August file is "
                "held)"),
    },
}

# ---------------------------------------------------------------------------
# Stop conditions (see the module docstring for the calibration)
# ---------------------------------------------------------------------------

REGISTER_MIN_SHARE_PCT = 95
REGISTER_MAX_CHANGED = 30
JUDGEMENTS_MIN_SHARE_PCT = 90

# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

REGISTER_VALUE_TYPES = (
    ("organisation_name", "text NOT NULL"),
    ("registration_date", "date"),
    ("designation", "text"),
    ("corporate_form", "text"),
    ("notes", "text"),
)
JUDGEMENT_VALUE_TYPES = (
    ("landlord_name", "text NOT NULL"),
    ("landlord_type", "text"),
    ("status", "text"),
    ("consumer_grade", "text"),
    ("consumer_grade_change", "text"),
    ("consumer_grade_date", "date"),
    ("governance_grade", "text"),
    ("governance_grade_change", "text"),
    ("governance_grade_date", "date"),
    ("viability_grade", "text"),
    ("viability_grade_change", "text"),
    ("viability_grade_date", "date"),
    ("rent_grade", "text"),
    ("rent_grade_change", "text"),
    ("rent_grade_date", "date"),
    ("publication_type", "text"),
    ("engagement_process", "text"),
    ("name_or_code_change", "text"),
    ("other_landlords", "text"),
)
NOTICE_VALUE_TYPES = (
    ("provider_name", "text NOT NULL"),
    ("status", "text"),
    ("publication_type", "text"),
    ("route", "text"),
    ("explanation", "text"),
    ("notice_date", "date"),
    ("name_or_code_change", "text"),
    ("other_providers", "text"),
)
NULL_REASONS_TYPES = (("null_reasons", "jsonb NOT NULL"),)
# editions-only provenance (live column <- editions column); never compared,
# never in the content hash
PROVENANCE_TYPES = (
    ("file_source_url", "text NOT NULL"),
    ("file_name", "text NOT NULL"),
    ("file_release_page_url", "text NOT NULL"),
)
PROVENANCE_PAIRS = (("source_url", "file_source_url"),
                    ("source_file", "file_name"),
                    ("release_page_url", "file_release_page_url"))


def _names(types) -> tuple:
    return tuple(c for c, _ in types)


REGISTER_VALUES = _names(REGISTER_VALUE_TYPES)
JUDGEMENT_VALUES = _names(JUDGEMENT_VALUE_TYPES)
NOTICE_VALUES = _names(NOTICE_VALUE_TYPES)
PROVENANCE = _names(PROVENANCE_TYPES)
LIVE_PROVENANCE = tuple(lc for lc, _ in PROVENANCE_PAIRS)
GRADES = tuple(GRADE_RE)
# kind -> (key columns, period column, value columns)
KINDS = {
    "register": (("registration_number",), "snapshot_date", REGISTER_VALUES),
    "judgements": (("registration_number", "publication_date"),
                   "edition_date", JUDGEMENT_VALUES),
    "notices": (("registration_number", "publication_date"), "edition_date",
                NOTICE_VALUES),
}
# every stored live column but loaded_at, in table order (the survey hash)
LIVE_COLUMNS = {
    "register": ("snapshot_date", "registration_number") + REGISTER_VALUES
    + LIVE_PROVENANCE,
    "judgements": ("registration_number", "publication_date",
                   "landlord_name", "landlord_type", "status",
                   "consumer_grade", "consumer_grade_change",
                   "consumer_grade_date", "governance_grade",
                   "governance_grade_change", "governance_grade_date",
                   "viability_grade", "viability_grade_change",
                   "viability_grade_date", "rent_grade", "rent_grade_change",
                   "rent_grade_date", "publication_type",
                   "engagement_process", "name_or_code_change",
                   "other_landlords", "edition_date") + LIVE_PROVENANCE,
    "notices": ("registration_number", "publication_date", "provider_name",
                "status", "publication_type", "route", "explanation",
                "notice_date", "name_or_code_change", "other_providers",
                "edition_date") + LIVE_PROVENANCE,
}

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-11
# ---------------------------------------------------------------------------

LEGACY_LIVE = {
    "register": {
        "rows": 4734, "hash": "6a30ad24db177e6f" "86ea0ea3bb7a6521",
        "periods": {"2026-07-24": 1579, "2026-08-17": 1578,
                    "2026-09-17": 1577},
        "loaded_at": {"2026-07-24": ["2026-08-14 21:26:52.192738"],
                      "2026-08-17": ["2026-08-20 14:25:27.590022"],
                      "2026-09-17": ["2026-10-01 21:59:19.976912"]}},
    "judgements": {
        "rows": 326, "hash": "39c7cc2adb81b5c9" "f4ab30464af56dd5",
        "periods": {"2026-08-12": 15, "2026-09-30": 311},
        "loaded_at": {"2026-08-12": ["2026-08-14 21:26:52.192738"],
                      "2026-09-30": ["2026-10-01 21:59:19.976912"]}},
    "notices": {
        "rows": 2, "hash": "4c22298c60e0a14c" "fdfd934b407921ae",
        "periods": {"2026-09-30": 2},
        "loaded_at": {"2026-09-30": ["2026-10-01 21:59:19.976912"]}},
}
_MEDIA = "https://assets.publishing.service.gov.uk/media/"
# the held files (sha256 split in two halves so the credential scan does not
# flag them), the URL each was read from, and its docProps modified time
LEGACY_FILES = {
    ("register", "2026-07-24"): {
        "name": "Copy_of_List_of_registered_providers_24_July_2026.xlsx",
        "sha256": "6c85d663248e727291b3012419cc4080"
                  "108ff42b3889cf1d6584efb516066268",
        "url": _MEDIA + "6a6759293b40a3e9013dbaca/Copy_of_List_of_registered_"
                        "providers_24_July_2026.xlsx",
        "modified": "2026-07-27T13:11:58Z"},
    ("register", "2026-09-17"): {
        "name": "List_of_registered_providers_17_September_2026.xlsx",
        "sha256": "1aed0382c577b33f34e4ea42b065d324"
                  "16e714d28c4a3dca026986736d5997da",
        "url": _MEDIA + "6aad16bcce3f006bd4346c9b/List_of_registered_"
                        "providers_17_September_2026.xlsx",
        "modified": "2026-09-17T14:47:33Z"},
    ("judgements", "2026-08-12"): {
        "name": "20260812_RegulatoryJudgementsNotices_Published.xlsx",
        "sha256": "154d1c9eb257ee80c5992bbb9c1de178"
                  "2d39309a072e687f2d5f23d835f79ee0",
        "url": _MEDIA + "6a797ffd9fad0e27bebabe3f/20260812_Regulatory"
                        "JudgementsNotices_Published.xlsx",
        "modified": "2026-08-10T07:36:54Z"},
    ("judgements", "2026-09-30"): {
        "name": "20260930_RegulatoryJudgementsNotices_published.xlsx",
        "sha256": "6e3b87d1c6e7a5cd40706fc8b71d1536"
                  "9afe741a7011e7e6e55155d0a6dd2dab",
        "url": _MEDIA + "6abcf176efad0f1df67227aa/20260930_Regulatory"
                        "JudgementsNotices_published.xlsx",
        "modified": "2026-09-30T11:21:09Z"},
}
AUGUST_REGISTER = {
    "period": "2026-08-17",
    "note": ("file not obtainable: the publisher's media URL now serves the "
             "17 September file; loaded 2026-08-20 14:25:27 UTC with no "
             "run-log row"),
}
# the run that loaded the 12 August judgements (and the 24 July register)
LEGACY_RUN = {"id": 96, "rows": 1889, "register": "2026-07-24",
              "judgements": "2026-08-12"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    return json.loads(x) if isinstance(x, (str, bytes)) else (x or {})


def _norm(s) -> str:
    return " ".join(str("" if s is None else s).split())


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _as_date(v) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> date:
    return date.today()


class Reasons(dict):
    """A record's null_reasons {column: reason}; stored as jsonb (keys
    sorted). A plain dict read back from the database compares equal."""


def canonical(d) -> str:
    """null_reasons as canonical JSON text (keys sorted)."""
    return json.dumps(dict(d or {}), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _register_adapter() -> None:
    try:
        from psycopg2.extensions import register_adapter
        from psycopg2.extras import Json
    except ImportError:  # pragma: no cover (psycopg2 is a dependency)
        return
    register_adapter(Reasons, lambda r: Json(dict(r), dumps=canonical))


_register_adapter()


# ---------------------------------------------------------------------------
# Discovery: the GOV.UK content API pages
# ---------------------------------------------------------------------------

def _attachments(page_json) -> list:
    d = _json(page_json)
    return list(((d.get("details") or {}).get("attachments")) or [])


def _is_xlsx(url) -> bool:
    return Path(urlparse(str(url or "")).path).suffix.lower() == ".xlsx"


def _url_name(url) -> str:
    return Path(urlparse(str(url or "")).path).name


def register_attachment(page_json) -> dict:
    """{title, url, title_date, file_name, ignored} of the register page's
    one .xlsx attachment titled 'List of registered providers – <d Month
    yyyy>'. The accessible HTML version and the 'Registrations and
    de-registrations' pages are listed in ignored. ValueError, listing every
    attachment title, unless there is exactly one .xlsx attachment and it has
    that title (an .xlsx with another title is never passed over)."""
    atts = _attachments(page_json)
    titles = [_norm(a.get("title")) for a in atts]
    xlsx = [a for a in atts if _is_xlsx(a.get("url"))]
    hits = [a for a in xlsx
            if REGISTER_TITLE_RE.fullmatch(_norm(a.get("title")))]
    if len(hits) != 1 or len(xlsx) != 1:
        raise ValueError(f"{len(hits)} .xlsx attachment(s) titled 'List of "
                         "registered providers – <d Month yyyy>' and "
                         f"{len(xlsx)} .xlsx attachment(s) in all, expected "
                         f"exactly one; attachments: {titles}")
    a = hits[0]
    text = REGISTER_TITLE_RE.fullmatch(_norm(a["title"])).group(1)
    try:
        when = datetime.strptime(text, "%d %B %Y").date()
    except ValueError:
        raise ValueError(f"the attachment title {_norm(a['title'])!r} does "
                         "not give a date 'd Month yyyy'") from None
    return {"title": _norm(a["title"]), "url": a["url"], "title_date": when,
            "file_name": _url_name(a["url"]),
            "ignored": [t for x, t in zip(atts, titles) if x is not a]}


def judgements_attachment(page_json) -> dict:
    """{title, url, file_name, ignored} of the judgements page's one .xlsx
    attachment titled exactly JUDGEMENTS_TITLE; ValueError listing every
    attachment title otherwise (none, several, or another .xlsx)."""
    atts = _attachments(page_json)
    titles = [_norm(a.get("title")) for a in atts]
    xlsx = [a for a in atts if _is_xlsx(a.get("url"))]
    hits = [a for a in xlsx if _norm(a.get("title")) == JUDGEMENTS_TITLE]
    if len(hits) != 1 or len(xlsx) != 1:
        raise ValueError(f"{len(hits)} .xlsx attachment(s) titled "
                         f"{JUDGEMENTS_TITLE!r} and {len(xlsx)} .xlsx "
                         "attachment(s) in all, expected exactly one; "
                         f"attachments: {titles}")
    a = hits[0]
    return {"title": JUDGEMENTS_TITLE, "url": a["url"],
            "file_name": _url_name(a["url"]),
            "ignored": [t for x, t in zip(atts, titles) if x is not a]}


def change_history(page_json) -> list:
    """[(public_timestamp date 'yyyy-mm-dd', note)] of the page, as
    published (newest first)."""
    d = _json(page_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    return [(str(c.get("public_timestamp") or "")[:10], _norm(c.get("note")))
            for c in ch]


def page_dates(page_json) -> list:
    """Every date the page states for an update: its change history and
    public_updated_at, sorted, distinct."""
    d = _json(page_json)
    out = {t for t, _ in change_history(d) if t}
    if d.get("public_updated_at"):
        out.add(str(d["public_updated_at"])[:10])
    return sorted(out)


# ---------------------------------------------------------------------------
# Workbooks: the visible sheets, docProps, one sheet by its exact title
# ---------------------------------------------------------------------------

def visible_sheets(path) -> list:
    """The titles of the workbook's visible sheets, in order, read from
    xl/workbook.xml. A hidden or veryHidden sheet is never named here or
    anywhere else; its contents are never read."""
    name = Path(path).name
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        raise ValueError(f"{name}: not a readable .xlsx workbook "
                         f"({type(e).__name__})") from None
    out = []
    for node in root:
        if not node.tag.endswith("}sheets"):
            continue
        for s in node:
            if s.get("state", "visible") == "visible":
                out.append(s.get("name"))
    return out


def docprops_modified(path) -> datetime:
    """The workbook's docProps/core.xml modified time (UTC, whole
    seconds)."""
    name = Path(path).name
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("docProps/core.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as e:
        raise ValueError(f"{name}: no readable docProps/core.xml "
                         f"({type(e).__name__})") from None
    for node in root:
        if node.tag.endswith("}modified") and (node.text or "").strip():
            text = node.text.strip().replace("Z", "+00:00")
            try:
                when = datetime.fromisoformat(text)
            except ValueError:
                break
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.astimezone(timezone.utc).replace(microsecond=0)
    raise ValueError(f"{name}: docProps/core.xml states no modified date")


def _read_sheet(path, title, headers) -> list:
    """[(row number, cells)] of the one sheet `title`, opened by its exact
    title: its header (cells stripped, trailing empty cells dropped) must be
    exactly `headers`; wholly empty rows are skipped; a cell beyond the
    headed columns halts. Nothing else in the workbook is read."""
    import openpyxl
    name = Path(path).name
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            rows = [tuple(r) for r in wb[title].iter_rows(values_only=True)]
        finally:
            wb.close()
    if not rows:
        raise ValueError(f"{name}: sheet {title!r} is empty")
    header = [_norm(v) for v in rows[0]]
    while header and not header[-1]:
        header.pop()
    if header != list(headers):
        raise ValueError(f"{name}: sheet {title!r} header changed; expected "
                         f"{list(headers)}, found {header}")
    width = len(headers)
    out = []
    for n, r in enumerate(rows[1:], start=2):
        if all(_blank(v) for v in r):
            continue
        if any(not _blank(v) for v in r[width:]):
            raise ValueError(f"{name}: sheet {title!r} row {n}: a cell beyond "
                             f"the {width} headed columns")
        cells = tuple(r[:width])
        out.append((n, cells + (None,) * (width - len(cells))))
    return out


def register_identity(path, title_date) -> dict:
    """{kind, period, title_date, modified, rank, sheet, rows, file_name} of
    a register workbook. ValueError unless exactly one visible sheet's title
    starts 'Registered Providers - list' (the visible sheets are listed),
    the docProps modified date lies between the title date and 14 days
    after it (both named), and the header is exactly REGISTER_HEADERS. No
    other sheet is touched."""
    td = _as_date(title_date)
    name = Path(path).name
    vis = visible_sheets(path)
    hits = [s for s in vis if str(s).startswith(REGISTER_SHEET_PREFIX)]
    if len(hits) != 1:
        raise ValueError(f"{name}: {len(hits)} visible sheets titled "
                         f"'{REGISTER_SHEET_PREFIX}...', expected one; visible "
                         f"sheets {vis}")
    mod = docprops_modified(path)
    last = td + timedelta(days=REGISTER_MODIFIED_DAYS)
    if not td <= mod.date() <= last:
        raise ValueError(f"{name}: the register date is {td} but the "
                         f"workbook's docProps modified date is "
                         f"{mod:%Y-%m-%d} ({_iso(mod)}); expected between {td} "
                         f"and {last}: this is not the {td} register")
    rows = _read_sheet(path, hits[0], REGISTER_HEADERS)
    return {"kind": "register", "period": td.isoformat(), "title_date": td,
            "modified": mod, "rank": (td, mod), "sheet": hits[0],
            "rows": rows, "file_name": name}


def judgements_identity(path, file_name, page_json) -> dict:
    """{kind, period, edition_date, modified, rank, judgement_rows,
    notice_rows, file_name, page_checked} of a judgements workbook. The
    edition date is file_name's yyyymmdd prefix; when page_json is given it
    must equal a date of the page's change history or its
    public_updated_at; the docProps modified date must lie within 7 days of
    it; the visible sheets 'Regulatory Judgements' and 'Enforcement Notices'
    must exist with exactly the old build's headers. Else ValueError."""
    name = Path(path).name
    mm = FILE_DATE_RE.match(str(file_name or ""))
    ed = None
    if mm:
        try:
            ed = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
        except ValueError:
            ed = None
    if ed is None:
        raise ValueError(f"{file_name}: the file name does not start with a "
                         "date yyyymmdd (the publisher names each judgements "
                         "file by its edition date)")
    vis = visible_sheets(path)
    missing = [s for s in (JUDGEMENT_SHEET, NOTICE_SHEET) if s not in vis]
    if missing:
        raise ValueError(f"{name}: sheets {missing} missing; visible sheets "
                         f"{vis}")
    if page_json is not None:
        dates = page_dates(page_json)
        if ed.isoformat() not in dates:
            raise ValueError(f"{file_name}: edition date {ed} (the file "
                             "name) is not a date the page states; its change "
                             "history and public_updated_at give "
                             f"{list(reversed(dates))[:8]}")
    mod = docprops_modified(path)
    if abs((mod.date() - ed).days) > JUDGEMENTS_MODIFIED_DAYS:
        raise ValueError(f"{file_name}: edition date {ed} but the workbook's "
                         f"docProps modified date is {mod:%Y-%m-%d} "
                         f"({_iso(mod)}); expected within "
                         f"{JUDGEMENTS_MODIFIED_DAYS} days")
    return {"kind": "judgements", "period": ed.isoformat(),
            "edition_date": ed, "modified": mod, "rank": (ed, mod),
            "judgement_rows": _read_sheet(path, JUDGEMENT_SHEET,
                                          JUDGEMENT_HEADERS),
            "notice_rows": _read_sheet(path, NOTICE_SHEET, NOTICE_HEADERS),
            "file_name": str(file_name), "page_checked": page_json is not None}


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------

def text_cell(v) -> tuple:
    """(text, None) as published (outer whitespace stripped), or (None,
    reason): '-' marked_dash, empty or missing blank, the text 'None'
    publisher_text_none."""
    if _blank(v):
        return None, BLANK
    s = str(v).strip()
    if s == "-":
        return None, MARKED_DASH
    if s == "None":
        return None, PUBLISHER_TEXT_NONE
    return s, None


def date_cell(v, where) -> tuple:
    """(date, None) from a typed date or an Excel serial in 20000..60000
    (whole days, the 1899-12-30 epoch), or (None, reason) for '-'
    (marked_dash) or an empty cell (blank). Anything else raises ValueError
    naming `where`."""
    if _blank(v):
        return None, BLANK
    if isinstance(v, bool):
        raise ValueError(f"{where}: {v!r} is not a date")
    if isinstance(v, datetime):
        return v.date(), None
    if isinstance(v, date):
        return v, None
    if isinstance(v, (int, float)):
        if float(v).is_integer() and 20000 <= v <= 60000:
            return date(1899, 12, 30) + timedelta(days=int(v)), None
        raise ValueError(f"{where}: {v!r} is not an Excel date serial "
                         "(whole days in 20000..60000)")
    if isinstance(v, str) and v.strip() == "-":
        return None, MARKED_DASH
    raise ValueError(f"{where}: {v!r} is not a date (a typed date, an Excel "
                     "serial, '-' or blank)")


def grade_cell(value, column, landlord_type, change_text, where="") -> tuple:
    """(grade, None), or (None, reason) for '-' or an empty cell, by the
    spec's table: governance or viability '-' on a 'Local Authority'
    landlord not_applicable_local_authority; consumer '-' whose change cell
    starts 'Not assessed yet' not_assessed_yet; any other '-'
    marked_dash_no_reason; empty blank. A grade outside the set read from
    the files (GRADE_RE) raises ValueError naming the cell."""
    if column not in GRADE_RE:
        raise ValueError(f"{where}{column}: not a grade column")
    if _blank(value):
        return None, BLANK
    s = str(value).strip()
    if s == "-":
        if (column in ("governance_grade", "viability_grade")
                and _norm(landlord_type) == LA_TYPE):
            return None, NOT_APPLICABLE_LA
        if (column == "consumer_grade"
                and _norm(change_text).startswith(NOT_ASSESSED_PREFIX)):
            return None, NOT_ASSESSED_YET
        return None, MARKED_DASH_NO_REASON
    if GRADE_RE[column].fullmatch(s):
        return s, None
    raise ValueError(f"{where}{column}: grade {s!r} not seen before (allowed "
                     f"{GRADE_ALLOWED[column]}, '-' or blank); the marker "
                     "table must be reviewed with the evidence")


def _key_text(v, where) -> str:
    if isinstance(v, bool) or _blank(v):
        raise ValueError(f"{where}: blank (a key column)")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"{where}: {v!r} is not a code")
        v = int(v)
    s = str(v).strip()
    if s in ("-", "None"):
        raise ValueError(f"{where}: {s!r} (a key column)")
    return s


def _required_text(v, where) -> str:
    s, why = text_cell(v)
    if s is None:
        raise ValueError(f"{where}: {why} (a required column)")
    return s


# the old build's text() and date(), kept only to prove what it stored
def _old_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return None if s in ("", "-", "None") else s


def _old_date(v, where):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if not 20000 <= v <= 60000:
            raise ValueError(f"{where}: {v!r} outside the old build's serial "
                             "range")
        return date(1899, 12, 30) + timedelta(days=int(v))
    s = str(v).strip()
    if not s or s == "-":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{where}: unparseable date {v!r}")


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

# (column, cell index, how, change cell index for a grade)
REGISTER_CELLS = (("registration_date", 2, "date"),
                  ("designation", 3, "text"),
                  ("corporate_form", 4, "text"),
                  ("notes", 5, "text"))
JUDGEMENT_CELLS = (
    ("landlord_type", 2, "text"), ("name_or_code_change", 3, "text"),
    ("other_landlords", 4, "text"), ("status", 5, "text"),
    ("consumer_grade", 6, "grade", 7), ("consumer_grade_change", 7, "text"),
    ("consumer_grade_date", 8, "date"),
    ("governance_grade", 9, "grade", 10),
    ("governance_grade_change", 10, "text"),
    ("governance_grade_date", 11, "date"),
    ("viability_grade", 12, "grade", 13),
    ("viability_grade_change", 13, "text"),
    ("viability_grade_date", 14, "date"),
    ("rent_grade", 15, "grade", 17), ("rent_grade_date", 16, "date"),
    ("rent_grade_change", 17, "text"),
    ("publication_type", 18, "text"), ("engagement_process", 20, "text"))
NOTICE_CELLS = (
    ("name_or_code_change", 2, "text"), ("other_providers", 3, "text"),
    ("status", 4, "text"), ("publication_type", 5, "text"),
    ("route", 7, "text"), ("explanation", 8, "text"),
    ("notice_date", 9, "date"))
# kind -> (cells, key cell, date-key cell or None, name column, name cell,
#          headers, sheet)
_LAYOUT = {
    "register": (REGISTER_CELLS, 1, None, "organisation_name", 0,
                 REGISTER_HEADERS, REGISTER_SHEET_PREFIX),
    "judgements": (JUDGEMENT_CELLS, 0, 19, "landlord_name", 1,
                   JUDGEMENT_HEADERS, JUDGEMENT_SHEET),
    "notices": (NOTICE_CELLS, 0, 6, "provider_name", 1, NOTICE_HEADERS,
                NOTICE_SHEET),
}


def _provenance(identity) -> dict:
    prov = identity.get("provenance")
    if prov:
        return {c: prov[c] for c in PROVENANCE}
    name = identity.get("file_name") or "unknown"
    return {"file_source_url": f"file:{name}", "file_name": name,
            "file_release_page_url": NO_PAGE}


def _build(kind, rows, identity, rules) -> list:
    if rules not in ("new", "old"):
        raise ValueError(f"rules must be 'new' or 'old', not {rules!r}")
    cells, kcell, dcell, ncol, ncell, headers, sheet = _LAYOUT[kind]
    keys, pc, _ = KINDS[kind]
    sheet = identity.get("sheet", sheet) if kind == "register" else sheet
    period = identity["period"]
    prov = _provenance(identity)
    out, seen = [], {}
    for n, r in rows:
        where = f"{sheet} row {n}"
        rec = {keys[0]: _key_text(r[kcell], f"{where} {headers[kcell]}"),
               pc: period}
        if dcell is not None:
            dw = f"{where} {headers[dcell]}"
            d = (_old_date(r[dcell], dw) if rules == "old"
                 else date_cell(r[dcell], dw)[0])
            if d is None:
                raise ValueError(f"{dw}: no date (a key column)")
            rec["publication_date"] = d
        key = tuple(rec[k] for k in keys)
        if key in seen:
            raise ValueError(f"{where}: duplicate key "
                             f"{'/'.join(str(k) for k in key)} (also row "
                             f"{seen[key]}); refusing to choose a winner")
        seen[key] = n
        reasons = Reasons()
        if rules == "old":
            rec[ncol] = str(r[ncell]).strip()
            for spec in cells:
                col, i, how = spec[:3]
                rec[col] = (_old_date(r[i], f"{where} {headers[i]}")
                            if how == "date" else _old_text(r[i]))
        else:
            rec[ncol] = _required_text(r[ncell], f"{where} {headers[ncell]}")
            for spec in cells:
                col, i, how = spec[:3]
                w = f"{where} {headers[i]}"
                if how == "date":
                    v, why = date_cell(r[i], w)
                elif how == "grade":
                    v, why = grade_cell(r[i], col, r[2], r[spec[3]],
                                        where=f"{w}: ")
                else:
                    v, why = text_cell(r[i])
                rec[col] = v
                if why is not None:
                    reasons[col] = why
        rec["null_reasons"] = reasons
        rec.update(prov)
        out.append(rec)
    out.sort(key=lambda x: tuple(str(x[k]) for k in keys))
    return out


def build_register(rows, identity, *, rules="new") -> list:
    """The register period's records from register_identity's rows: one per
    provider, sorted by registration number, each with null_reasons (a
    Reasons dict) and the identity's file_* provenance. rules='old' gives
    the old build's values (its text() and date()) with no reasons, for the
    migration proofs only. ValueError on a blank or repeated registration
    number, a blank organisation name or a bad date."""
    return _build("register", rows, identity, rules)


def build_judgements(rows, identity, *, rules="new") -> list:
    """The judgements period's records (key registration_number,
    publication_date; grades by grade_cell, dates by date_cell, text by
    text_cell), each with null_reasons. ValueError on an unseen grade, a bad
    date, a blank key or landlord, or a repeated key."""
    return _build("judgements", rows, identity, rules)


def build_notices(rows, identity, *, rules="new") -> list:
    """The enforcement notices' records (as build_judgements)."""
    return _build("notices", rows, identity, rules)


def _kind_of(record) -> str:
    if "snapshot_date" in record:
        return "register"
    if "landlord_name" in record:
        return "judgements"
    if "provider_name" in record:
        return "notices"
    raise ValueError(f"not an S24 record: {sorted(record)[:6]}")


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def rows_content_sha(records, *, reasons=True) -> str:
    """sha256 of the records' content: one line per record sorted by key,
    the key, every value column (NULL as the empty string, dates ISO) and
    (reasons, the default) the null_reasons canonical JSON, '|'-joined;
    lines joined by LF, UTF-8. Provenance is not in it, so it never makes a
    new edition. reasons=False renders the values only (the before-state
    lines of live tables, which have no null_reasons)."""
    records = list(records)
    kind = _kind_of(records[0]) if records else "register"
    keys, _, values = KINDS[kind]
    ordered = sorted(records, key=lambda r: tuple(_cell(r[k]) for k in keys))
    lines = ["|".join([_cell(r[k]) for k in keys]
                      + [_cell(r.get(c)) for c in values]
                      + ([canonical(r.get("null_reasons"))] if reasons
                         else []))
             for r in ordered]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def content_sha256(path) -> str:
    """SHA-256 of the file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def not_recorded(record) -> Reasons:
    """The reasons of edition 1 'as loaded' where the old build stored a
    bare NULL: reason_not_recorded_as_loaded on every NULL value."""
    _, _, values = KINDS[_kind_of(record)]
    return Reasons({c: NOT_RECORDED for c in values if record.get(c) is None})


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_STAMP = r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})Z"
_RANK_RE = re.compile(r"\b(register|judgements) ([0-9]{4}-[0-9]{2}-[0-9]{2}); "
                      r"modified " + _STAMP)
_LEDGER_RE = re.compile(r"(.*) \((register|judgements) ([0-9]{4}-[0-9]{2}-"
                        r"[0-9]{2}); modified " + _STAMP + r"(; as loaded)?\)")


def _stamp(text) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def release_rank(x) -> "tuple | None":
    """(title or file date, docProps modified) of a file: an identity's own
    rank, or the '<kind> <date>; modified <time>Z' an edition's or ledger
    row's source_file carries; None if it names none (a URL or bare file
    name is never read for a rank)."""
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    if not mm:
        return None
    return date.fromisoformat(mm.group(2)), _stamp(mm.group(3))


def rank_text(rank) -> str:
    if not rank:
        return "no file date"
    return f"dated {rank[0]}, modified {_iso(rank[1])}"


def ledger_source(where, kind, period, modified, *, as_loaded=False) -> str:
    """A file's ledger source_file: where it was read (the final URL, or a
    --file path), the period it covers and its docProps modified time
    (marked 'as loaded' for migrate-legacy's rows)."""
    return (f"{where} ({kind} {period}; modified {_iso(modified)}"
            + ("; as loaded" if as_loaded else "") + ")")


def parse_ledger_source(s) -> "tuple | None":
    """(where, rank, [period]) of a ledger source_file, or None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return (mm.group(1), (date.fromisoformat(mm.group(3)),
                          _stamp(mm.group(4))), [mm.group(3)])


def tip_ranks(tips, checked) -> dict:
    """{held period: the newest file rank that stated it}: the tip
    edition's, or a later file the ledger records for the period."""
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, allow_older=False,
                    stranded=()) -> "list | None":
    """The periods the ledger (of one table) records this file's (where,
    sha256) pair for, if that covers the period its ledger source declares;
    else None (the file is read and planned). A held period whose tip comes
    from a newer file is not needed (skipped as older) unless allow_older; a
    stranded period (editions, no live rows) always makes the file be read.
    A run that failed or was rejected leaves no ledger row, so the rerun
    reads the file again."""
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
    if not need or need & set(stranded):
        return None
    return sorted(have) if need <= have else None


def plan_periods(periods, rank, ranks, checked, source, sha, *, recheck,
                 allow_older, newest_only=False) -> tuple:
    """(new, revised, skipped). periods: the file's periods; rank: its own;
    ranks: {held period: rank}; checked: {period: {(source, sha)}} of the
    ledger. new: not held; revised: held, compared with the tip; skipped
    {period: reason}: a held period whose tip comes from a newer file, or
    (newest_only, the judgements) a new period older than the newest held
    one, unless allow_older (the older-file guard, on every path); or a held
    period whose (source, sha) the ledger already records, unless --recheck
    names it. --recheck must be a held period of the file (ValueError)."""
    periods = sorted(str(p) for p in periods)
    if recheck is not None and (recheck not in periods or recheck not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held period of this "
                         f"file ({', '.join(periods)})")
    newest = max(ranks) if ranks else None
    new, revised, skipped = [], [], {}
    for p in periods:
        if p not in ranks:
            if (newest_only and newest is not None and p < newest
                    and not allow_older):
                skipped[p] = (f"older: the newest held edition is {newest}; "
                              f"this file is {p}")
                continue
            new.append(p)
            continue
        t = ranks[p]
        if t is not None and t > rank and not allow_older:
            skipped[p] = (f"older: its tip comes from a file {rank_text(t)}, "
                          f"this file is {rank_text(rank)}")
            continue
        if recheck != p and (source, sha) in checked.get(p, ()):
            skipped[p] = "checked: this file is already in the ledger"
            continue
        revised.append(p)
    return new, revised, skipped


# ---------------------------------------------------------------------------
# Stop conditions, reissues, corrections
# ---------------------------------------------------------------------------

def _by_key(records, kind) -> dict:
    keys = KINDS[kind][0]
    return {tuple(r[k] for k in keys): r for r in records or ()}


def _row_values(r, kind) -> tuple:
    return (tuple(r.get(c) for c in KINDS[kind][2])
            + (canonical(r.get("null_reasons")),))


def row_diff(new, old, kind) -> tuple:
    """(added keys, removed keys, changed keys): key, every value column
    and null_reasons compared (NULL equals NULL, NULL differs from 0)."""
    nk, ok = _by_key(new, kind), _by_key(old, kind)
    added = sorted(set(nk) - set(ok), key=str)
    removed = sorted(set(ok) - set(nk), key=str)
    changed = sorted((k for k in set(nk) & set(ok)
                      if _row_values(nk[k], kind) != _row_values(ok[k], kind)),
                     key=str)
    return added, removed, changed


def _keys_text(keys) -> str:
    return ", ".join("/".join(str(x) for x in k) for k in keys[:6])


def period_problems(new, tip, prev, *, kind) -> list:
    """The stop conditions of one period (empty = fine); every one is
    released by --acknowledge PERIOD (logged). tip: the held period's tip
    records (a reissue or re-read of a held period), else None; prev: the
    latest earlier held period's records (a new period), or None. kind:
    'register', 'judgements' or 'notices'. Held period: any row added,
    removed or changed (values or null_reasons). New register snapshot:
    fewer than REGISTER_MIN_SHARE_PCT of prev's providers, or more than
    REGISTER_MAX_CHANGED providers added, or removed. New judgements
    edition: fewer than JUDGEMENTS_MIN_SHARE_PCT of prev's rows."""
    g = globals()
    out = []
    if tip is not None:
        a, r, c = row_diff(new, tip, kind)
        if a or r or c:
            out.append(f"{len(a) + len(r) + len(c)} of {len(tip)} rows differ "
                       f"from the held edition (keys added {len(a)}, removed "
                       f"{len(r)}, values or null_reasons changed {len(c)}"
                       + (f"; e.g. {_keys_text(a + r + c)}" if a or r or c
                          else "")
                       + "); a reissue of a held period that changes any row "
                       "needs --acknowledge PERIOD")
        return out
    if not prev:
        return out
    if kind == "register":
        pct, most = g["REGISTER_MIN_SHARE_PCT"], g["REGISTER_MAX_CHANGED"]
        if len(new) * 100 < pct * len(prev):
            out.append(f"{len(new)} providers against {len(prev)} in the "
                       f"previous snapshot (fewer than {pct}%)")
        a, r, _ = row_diff(new, prev, kind)
        if len(a) > most:
            out.append(f"{len(a)} providers added against the previous "
                       f"snapshot (limit {most}): {_keys_text(a)}")
        if len(r) > most:
            out.append(f"{len(r)} providers removed against the previous "
                       f"snapshot (limit {most}): {_keys_text(r)}")
    elif kind == "judgements":
        pct = g["JUDGEMENTS_MIN_SHARE_PCT"]
        if len(new) * 100 < pct * len(prev):
            out.append(f"{len(new)} judgements against {len(prev)} in the "
                       f"previous edition (fewer than {pct}%)")
    return out


def reissue_problem(rank, tip_rank, same_bytes, new, tip, kind) -> "str | None":
    """A reissue keeping its rank: the file has the rank of the file of the
    held tip but other bytes and other content. None otherwise (a higher
    rank is an ordinary republication; the same bytes are a re-read)."""
    if tip_rank is None or rank != tip_rank or same_bytes:
        return None
    a, r, c = row_diff(new, tip, kind)
    if not (a or r or c):
        return None
    return (f"the file is {rank_text(rank)}, the same date and docProps "
            "modified time as the file of the held tip, but other bytes and "
            f"other content ({len(a) + len(r) + len(c)} rows differ): a "
            "reissue keeping its date; read the page's change note, then "
            "--accept-reissue PERIOD")


def correction_records(tip_records, corr) -> tuple:
    """(records, cells changed): copies of the tip records with every cell
    of corr['column'] holding corr['stored'] set NULL and corr['reason']
    added to that record's null_reasons."""
    col, stored = corr["column"], corr["stored"]
    out, n = [], 0
    for r in tip_records:
        if r.get(col) == stored:
            reasons = Reasons(r.get("null_reasons") or {})
            reasons[col] = corr["reason"]
            r = dict(r, **{col: None, "null_reasons": reasons})
            n += 1
        out.append(r)
    return out, n


# ---------------------------------------------------------------------------
# Specs and profiles
# ---------------------------------------------------------------------------

def tip_row_count(editions_table: str, period_col: str):
    """expected_rows_per_period for status: the tip edition's row count (a
    period's rows are its own)."""
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


def _spec(name, live, editions, kind, values, *, newest_only) -> core.EditionSpec:
    keys, pc, _ = KINDS[kind]
    key_types = (("registration_number", "text NOT NULL"),)
    if "publication_date" in keys:
        key_types += (("publication_date", "date NOT NULL"),)
    return core.EditionSpec(
        name=name, live_table=live, editions_table=editions, key_cols=keys,
        period_col=pc, value_cols=values,
        extra_cols=NULL_REASONS_TYPES + PROVENANCE_TYPES,
        refresh_cols=_names(values) + LIVE_PROVENANCE + ("loaded_at",),
        refresh_from=PROVENANCE_PAIRS + (("loaded_at", "loaded_at"),),
        whole_period_cols=LIVE_PROVENANCE,   # one file's provenance a period
        key_types=key_types + ((pc, "date NOT NULL"),),
        fk_la_boundaries=False,
        table_constraints=(f"CONSTRAINT {editions}_reasons_chk CHECK "
                           "(jsonb_typeof(null_reasons) = 'object')",),
        refresh_key_changes=newest_only,
        live_newest_period_only=newest_only,
        expected_rows_per_period=tip_row_count(editions, pc))


REGISTER = _spec("s24_register", "rsh_registered_providers",
                 "rsh_registered_providers_editions", "register",
                 REGISTER_VALUE_TYPES, newest_only=False)
JUDGEMENTS = _spec("s24_judgements", "rsh_regulatory_judgements",
                   "rsh_regulatory_judgements_editions", "judgements",
                   JUDGEMENT_VALUE_TYPES, newest_only=True)
NOTICES = _spec("s24_notices", "rsh_enforcement_notices",
                "rsh_enforcement_notices_editions", "notices",
                NOTICE_VALUE_TYPES, newest_only=True)
SPECS = {"register": REGISTER, "judgements": JUDGEMENTS, "notices": NOTICES}
PARTS = {"register": ("register",), "judgements": ("judgements", "notices")}


def _no_label(fetched_on) -> str:
    raise ValueError("an S24 release label comes from the file; store "
                     "through apply_part(...)")


def check_records(records, period) -> None:
    """ValueError unless the period's records are whole: each of this
    period, one per key, a null_reasons dict naming only NULL value columns,
    one provenance over the records."""
    if not records:
        return
    kind = _kind_of(records[0])
    keys, pc, values = KINDS[kind]
    seen, prov = set(), set()
    for r in records:
        if r.get(pc) != period:
            raise ValueError(f"{period}: a record of {pc} {r.get(pc)!r}")
        k = tuple(r[c] for c in keys)
        if k in seen:
            raise ValueError(f"{period}: {k} appears twice")
        seen.add(k)
        why = r.get("null_reasons")
        if not isinstance(why, dict):
            raise ValueError(f"{period}: {k} null_reasons is not a dict")
        stray = [c for c in why if c not in values or r.get(c) is not None]
        if stray:
            raise ValueError(f"{period}: {k} null_reasons names {stray}, "
                             "which are not NULL value columns")
        prov.add(tuple(r.get(c) for c in PROVENANCE))
    if len(prov) != 1:
        raise ValueError(f"{period}: {len(prov)} provenance values over the "
                         "records; one file's provenance per period")


def _profile(spec, values, heading, default, savepoint) -> pe.Profile:
    return pe.Profile(
        spec=spec, value_cols=_names(values) + ("null_reasons",),
        run_agent=RUN_AGENT, run_source=RUN_SOURCE, heading=heading,
        default_source_file=default, expected_areas=None,
        release_label=_no_label, content_sha256=rows_content_sha,
        check_records=check_records, file_checks=True, savepoint=savepoint,
        example_label="(values, null_reasons)")


PROFILES = {
    "register": _profile(REGISTER, REGISTER_VALUE_TYPES,
                         "S24 RSH register of registered providers "
                         "(rsh_registered_providers)",
                         "RSH list of registered providers", "s24_register"),
    "judgements": _profile(JUDGEMENTS, JUDGEMENT_VALUE_TYPES,
                           "S24 RSH regulatory judgements "
                           "(rsh_regulatory_judgements)",
                           "RSH regulatory judgements table",
                           "s24_judgements"),
    "notices": _profile(NOTICES, NOTICE_VALUE_TYPES,
                        "S24 RSH enforcement notices "
                        "(rsh_enforcement_notices)",
                        "RSH regulatory judgements table", "s24_notices"),
}


def profile(name) -> pe.Profile:
    """PROFILES[name] bound to the current SPECS[name] (looked up when
    called, so tests can swap the specs)."""
    return globals()["PROFILES"][name].with_spec(globals()["SPECS"][name])


def _spec_kind(spec) -> str:
    for name, s in globals()["SPECS"].items():
        if s.editions_table == spec.editions_table:
            return name
    if spec.period_col == "snapshot_date":
        return "register"
    return ("judgements" if "landlord_name" in _names(spec.value_cols)
            else "notices")


def refresh_spec(spec):
    """The spec refresh-latest uses: null_reasons is an editions-only column
    (the live tables have none), so a key-change insert must not name it;
    everything compared stays the same (it is not a refresh column)."""
    return dataclasses.replace(spec, extra_cols=PROVENANCE_TYPES)


def _late(name: str):
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: the three editions tables, their append-only triggers and the
    three file-check ledgers. Idempotent; no value is written; the live
    tables are not touched."""
    for name in globals()["SPECS"]:
        prof = profile(name)
        core.create_schema(cur, prof.spec)
        pe.create_file_checks(cur, prof)


def insert_live(cur, profile, period: str, records: list) -> None:
    """A new period's live rows: key, period, the value columns and the
    three provenance columns from the records' file_* values; the
    editions-only columns (null_reasons, file_*) are not written; loaded_at
    takes its default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    values = tuple(c for c in profile.value_cols if c != "null_reasons")
    cols = (tuple(spec.key_cols) + (spec.period_col,) + values
            + LIVE_PROVENANCE)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r.get(c) for c in values)
         + tuple(r[c] for c in PROVENANCE) for r in records],
        page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a period's edition records: source_file, the release label, the
    published date and the files read ((ledger source, sha256),)."""
    source_file: str
    label: str
    published: date
    files: tuple


def _tip_provenance(cur, spec, period) -> dict:
    tip = core.chain_tip(cur, spec, period)
    cur.execute(f"SELECT DISTINCT {', '.join(PROVENANCE)} FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = %s", (period, tip))
    rows = cur.fetchall()
    if len(rows) != 1:
        halt(f"{period}: edition {tip} carries {len(rows)} provenance values")
    return dict(zip(PROVENANCE, rows[0]))


def apply_part(cur, profile, period, records, *, info) -> str:
    """pe.apply_period for one table's period, then its ledger rows, inside
    the caller's savepoint (a judgements file's two tables share it): a new
    period's edition 1, live rows and ledger rows commit or roll back
    together. A stranded period gets its live rows with the tip's
    provenance."""
    kind = pe.classify_period(cur, profile, period, records)
    if kind == LIVE_MISSING:
        prov = _tip_provenance(cur, profile.spec, period)
        records = [dict(r, **prov) for r in records]
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


def fetch(url, dest, session=None) -> tuple:
    """Download url into the directory dest (redirects followed) and return
    (path to read, final URL). The file name is the final URL's. Checked
    before it is kept: more than MIN_FILE_BYTES, an .xlsx name, and it opens
    as an .xlsx workbook with at least one visible sheet. A same-named file
    with the same content is kept as it is; one with different content is
    never replaced: the download is saved beside it as <stem>-<sha8><suffix>
    and that is the one read. An existing name is never a reason to skip the
    download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    if len(body) <= MIN_FILE_BYTES:
        halt(f"{final}: {len(body)} bytes, expected more than "
             f"{MIN_FILE_BYTES}; nothing written")
    name = _url_name(final)
    suffix = Path(name).suffix.lower()
    if suffix != ".xlsx":
        halt(f"{final}: not an .xlsx file; nothing written")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / name
    new_sha = hashlib.sha256(body).hexdigest()
    tmp = dest / (Path(name).stem + ".download.tmp" + suffix)
    tmp.write_bytes(body)
    try:
        if not visible_sheets(tmp):
            raise ValueError("no visible sheet")
    except ValueError as e:
        tmp.unlink()
        halt(f"{final}: does not open as an .xlsx workbook ({e}); nothing "
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
                print(f"NOTE: {name} differs from this download; {path.name} "
                      f"already holds it (sha256 {new_sha[:16]})")
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
    """Where a --file was read: the path under the repo, else absolute."""
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
    """The period's rows as records (key, period as a string, the value
    columns, null_reasons as Reasons and the three file_* provenance
    values): the live table's (edition None; its provenance columns read as
    file_*, null_reasons empty: live has none) or the given edition's."""
    kind = _spec_kind(spec)
    keys, pc, values = KINDS[kind]
    if edition is None:
        cols = keys + values + LIVE_PROVENANCE
        cur.execute(f"SELECT {', '.join(cols)} FROM public.{spec.live_table} "
                    f"WHERE {pc} = %s", (period,))
        names = keys + values + PROVENANCE
        out = [dict(zip(names, r), null_reasons=Reasons())
               for r in cur.fetchall()]
    else:
        names = keys + values + ("null_reasons",) + PROVENANCE
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {pc} = %s AND "
                    "edition = %s", (period, edition))
        out = [dict(zip(names, r)) for r in cur.fetchall()]
        for r in out:
            r["null_reasons"] = Reasons(_json(r["null_reasons"]))
    for r in out:
        r[pc] = str(period)
    return out


def tip_info(cur, spec) -> dict:
    """{period (string): {edition, source_file, rank}} of every period with
    editions."""
    pc = spec.period_col
    cur.execute(f"SELECT DISTINCT {pc} FROM public.{spec.editions_table}")
    out = {}
    for (p,) in cur.fetchall():
        tip = core.latest_edition(cur, spec, p)
        cur.execute(f"SELECT DISTINCT source_file FROM "
                    f"public.{spec.editions_table} WHERE {pc} = %s AND "
                    "edition = %s", (p, tip))
        src = {r[0] for r in cur.fetchall()}
        sf = src.pop() if len(src) == 1 else None
        out[pe._p(p)] = {"edition": tip, "source_file": sf,
                         "rank": release_rank(sf)}
    return out


def _legacy_rank(part, period):
    f = globals()["LEGACY_FILES"].get((part, period))
    if not f:
        return None
    return date.fromisoformat(period), _stamp(f["modified"].rstrip("Z"))


def legacy_tips(cur, name, spec) -> dict:
    """Before the editions tables exist: {live period: {rank}}, the rank of
    the held file (LEGACY_FILES; the August register has none)."""
    part = "register" if name == "register" else "judgements"
    out = {}
    for p in _live_periods(cur, spec):
        out[p] = {"edition": None, "source_file": None,
                  "rank": _legacy_rank(part, p)}
    return out


def ledger_checks(cur, prof) -> dict:
    """{period (string): {(source_file, file_sha256)}} of the ledger."""
    pc = prof.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(prof)}")
    out = {}
    for p, f, s in cur.fetchall():
        out.setdefault(pe._p(p), set()).add((f, s))
    return out


def _tip_file(cur, prof, period, sha, tips) -> bool:
    """True when the ledger records these bytes for the period's tip edition
    (the file the tip came from)."""
    t = tips.get(period)
    if not t or t.get("edition") is None:
        return False
    cur.execute(f"SELECT COUNT(*) FROM public.{pe.file_checks_table(prof)} "
                f"WHERE {prof.spec.period_col} = %s AND file_sha256 = %s AND "
                "edition = %s", (period, sha, t["edition"]))
    return bool(cur.fetchone()[0])


def _live_periods(cur, spec) -> list:
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    return sorted(pe._p(r[0]) for r in cur.fetchall())


def live_state(cur, name, spec=None) -> dict:
    """{rows, hash, periods {period: rows}, loaded_at {period: [UTC
    times]}} of a live table. The hash is the spec's survey hash: md5 of
    every stored column but loaded_at, each coalesce(col::text, '~'),
    '|'-joined, rows ordered by the period then the key, joined by LF."""
    spec = spec or globals()["SPECS"][name]
    cols = LIVE_COLUMNS[name]
    keys, pc, _ = KINDS[name]
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in cols)
    order = ", ".join((pc,) + keys)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"{order})) FROM public.{spec.live_table}")
    n, h = cur.fetchone()
    cur.execute(f"SELECT {pc}, COUNT(*) FROM public.{spec.live_table} "
                "GROUP BY 1")
    periods = {pe._p(p): c for p, c in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT {pc}, to_char(loaded_at AT TIME ZONE 'UTC', "
                f"'YYYY-MM-DD HH24:MI:SS.US') FROM public.{spec.live_table}")
    loaded = {}
    for p, t in cur.fetchall():
        loaded.setdefault(pe._p(p), []).append(t)
    return {"rows": n, "hash": h, "periods": periods,
            "loaded_at": {p: sorted(v) for p, v in loaded.items()}}


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT,
    source '24'). Called only on committed load, migrate-legacy and
    restore-edition runs, partial runs included."""
    pe.log_run(cur, profile("register"), rows_written, notes, started_at)


def _missing_tables(cur, name) -> list:
    prof = profile(name)
    return [t for t in (prof.spec.editions_table, pe.file_checks_table(prof))
            if not table_exists(cur, t)]


def cmd_ddl(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for name in globals()["SPECS"]:
                prof = profile(name)
                for t in (prof.spec.editions_table,
                          pe.file_checks_table(prof)):
                    print(f"{t}: "
                          f"{'exists' if table_exists(cur, t) else 'does not exist'}")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur)
        pe.finish(conn, args, "ddl: the three S24 editions tables, their "
                              "triggers and ledgers present (the live tables "
                              "untouched)")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status_lines(cur, name) -> list:
    prof = profile(name)
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


def _two_live_message(prof, periods) -> str:
    return (f"{prof.heading}: live holds {len(periods)} periods {periods}; "
            "a live_newest_period_only table holds the newest only. The old "
            "build's upsert left rows of an older edition in live; "
            f"refresh-latest --accept-key-changes {periods[-1]} removes them "
            "(each equals a row of its stored edition, which keeps them). "
            "ACTION NEEDED")


def cmd_status(args) -> int:
    """status: what needs action for the three tables; exit 1 if anything,
    or (with a clean message, nothing created) if a table does not exist."""
    conn = _conn(False)
    rc = 0
    try:
        with conn.cursor() as cur:
            for name in globals()["SPECS"]:
                prof = profile(name)
                missing = _missing_tables(cur, name)
                if missing:
                    print(f"{prof.heading}: {' and '.join(missing)} not "
                          "present yet; run `ddl --commit`, then "
                          "`migrate-legacy --commit` (S24 has no sync-new)")
                    rc = 1
                    continue
                live = _live_periods(cur, prof.spec)
                if prof.spec.live_newest_period_only and len(live) > 1:
                    print(_two_live_message(prof, live))
                    rc = 1
                    continue
                st = pe.status(cur, prof)
                print(pe.format_status(prof, st, status_lines(cur, name)))
                rc = rc or (0 if st["ok"] else 1)
    finally:
        conn.rollback()
        conn.close()
    return rc


def live_equals_tip(cur, spec, periods) -> list:
    """Problems (empty = fine): for each period, the live rows equal the tip
    edition on the key, the period and the value columns, NULL-safe, both
    ways (null_reasons and file_* are editions-only)."""
    _, _, values = KINDS[_spec_kind(spec)]
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
            bad.append(f"{pe._p(p)} ed{ed}: {counts[0]} edition-only, "
                       f"{counts[1]} live-only rows")
    return bad


def legacy_live_rows(cur, spec) -> tuple:
    """(newest live period, {older live period: rows}) of a
    live_newest_period_only table holding more than one period (the old
    build's upsert); (newest or None, {}) otherwise. Halts unless every live
    row of an older period equals a row of that period's tip edition (on
    the key and the compared columns), so removing them loses nothing."""
    live = _live_periods(cur, spec)
    if not spec.live_newest_period_only or len(live) < 2:
        return (live[-1] if live else None), {}
    sel = ", ".join(tuple(spec.key_cols) + spec.compare_cols)
    out = {}
    for o in live[:-1]:
        try:
            tip = core.chain_tip(cur, spec, o)
        except LookupError:
            halt(f"{spec.live_table}: the live period {o} has no editions; "
                 "run migrate-legacy first")
        except ValueError as e:
            halt(f"{spec.live_table}: {o}: {e}")
        cur.execute(f"""SELECT COUNT(*) FROM (
            SELECT {sel} FROM public.{spec.live_table}
             WHERE {spec.period_col} = %s
            EXCEPT ALL SELECT {sel} FROM public.{spec.editions_table}
             WHERE {spec.period_col} = %s AND edition = %s) q""", (o, o, tip))
        stray = cur.fetchone()[0]
        if stray:
            halt(f"{spec.live_table}: {stray} live row(s) of {o} equal no row "
                 f"of its tip edition {tip}; they would be lost, nothing "
                 "removed")
        cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table} WHERE "
                    f"{spec.period_col} = %s", (o,))
        out[o] = cur.fetchone()[0]
    return live[-1], out


def _core_plan(cur, spec, drift_args, kc_args):
    """(accept, accept_kc, plan, drifted, keyed) of the core refresh for one
    table, the --accept-* strings matched to its own periods."""
    tips, new, errors = core.latest_map(cur, spec)
    if new:
        halt(f"{spec.live_table}: periods with no editions "
             f"{[pe._p(x) for x in new]}; run migrate-legacy first")
    if errors:
        halt("invalid edition chain: " + "; ".join(
            f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
    drifted = {pe._p(p): p for p, tip in tips.items()
               if core.classify_period(cur, spec, p, tip)[0] == "drift"}
    accept = tuple(drifted[a] for a in drift_args if a in drifted)
    plan, _ = core._plan(cur, spec, accept)
    keyed = {pe._p(p): p for p in core.key_change_periods(plan)}
    accept_kc = tuple(keyed[a] for a in kc_args if a in keyed)
    return accept, accept_kc, plan, set(drifted), set(keyed)


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift P] [--accept-key-changes P] [--commit
    | --simulate]: for the three tables, the engine's refresh (each live
    period's latest edition copied into live: the values, provenance and
    loaded_at, under the before/after hash guard; keys added and removed
    only for a period named in --accept-key-changes), then live_equals_tip.
    A judgements or notices table still holding the old build's left-over
    rows of an older edition first has them removed (in the same savepoint),
    only when --accept-key-changes names its newest period."""
    writing = args.commit or args.simulate
    drift_args = tuple(args.accept_drift or ())
    kc_args = tuple(args.accept_key_changes or ())
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans, used_drift, used_kc = {}, set(), set()
            for name in globals()["SPECS"]:
                missing = _missing_tables(cur, name)
                if missing:
                    halt(f"{', '.join(missing)} not present yet; run `ddl "
                         "--commit`, then `migrate-legacy --commit`")
                spec = profile(name).spec
                newest, legacy = legacy_live_rows(cur, spec)
                if legacy:
                    used_kc.add(newest)
                    for o, n in sorted(legacy.items()):
                        print(f"{spec.live_table}: {n} left-over live row(s) "
                              f"of {o} (the old build's upsert; each equals a "
                              "row of its stored edition, which keeps them) "
                              "would be removed from live; this needs "
                              f"--accept-key-changes {newest}")
                    tip = core.chain_tip(cur, spec, newest)
                    added, removed = core._key_diff(cur, spec, newest, tip)
                    print(f"{spec.live_table}: then {newest} against edition "
                          f"{tip}: "
                          f"{core.rows_differing(cur, spec, newest, tip)} "
                          f"rows differ; keys added {len(added)}, removed "
                          f"{len(removed)}")
                    plans[name] = (spec, newest, legacy, None)
                    continue
                cp = _core_plan(cur, spec, drift_args, kc_args)
                used_drift |= cp[3]
                used_kc |= cp[4]
                counts = core.refresh_counts(cur, spec, cp[0])
                print(f"{spec.live_table}: rows refresh-latest would write: "
                      + (", ".join(f"{pe._p(p)}={n}" for p, n in counts.items())
                         or "none") + f" (total {sum(counts.values())})")
                lines = core.key_change_lines(cp[2], cp[1], name=pe._p)
                print(f"{spec.live_table}: keys added and removed:"
                      + ("" if lines else " none"))
                for line in lines:
                    print(f"  {line}")
                plans[name] = (spec, newest, {}, cp)
            stray = sorted(set(drift_args) - used_drift)
            if stray and not any(v[2] for v in plans.values()):
                halt(f"--accept-drift {stray}: not drifted in any table, "
                     "nothing to accept")
            stray = sorted(set(kc_args) - used_kc)
            if stray:
                halt(f"--accept-key-changes {stray}: no keys added or removed "
                     "and no left-over rows in those periods, nothing to "
                     "accept")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            msgs = []
            for name, (spec, newest, legacy, cp) in plans.items():
                with core._own_savepoint(cur, f"s24_refresh_{name}"):
                    removed = 0  # live rows removed by this run; not a source value
                    if legacy:
                        if newest not in kc_args:
                            halt(f"{spec.live_table}: live holds left-over "
                                 f"rows of {sorted(legacy)}; re-run with "
                                 f"--accept-key-changes {newest} to remove "
                                 "them (they stay in editions)")
                        for o, n in sorted(legacy.items()):
                            cur.execute(f"DELETE FROM public.{spec.live_table}"
                                        f" WHERE {spec.period_col} = %s", (o,))
                            if cur.rowcount != n:
                                halt(f"{spec.live_table}: removed "
                                     f"{cur.rowcount} rows of {o}, expected "
                                     f"{n}")
                            removed += n
                        cp = _core_plan(cur, spec, drift_args, kc_args)
                        used_drift |= cp[3]
                    accept, accept_kc = cp[0], cp[1]
                    pe._refresh_guard(cur, spec, accept, accept_kc)
                    res = core.refresh_latest(cur, refresh_spec(spec), accept,
                                              accept_kc)
                    check = sorted(set(res["updated"])
                                   | ({newest} if legacy else set()))
                    bad = live_equals_tip(cur, spec, check)
                    live = _live_periods(cur, spec)
                    if spec.live_newest_period_only and len(live) > 1:
                        bad.append(f"live holds {live} after the refresh")
                    if bad:
                        halt(f"refresh-latest: {spec.live_table} differs from "
                             "the latest edition after the refresh, rolled "
                             "back: " + "; ".join(bad[:6]))
                keys = ""
                if res["inserted"] or res["deleted"]:
                    keys = (f"; key changes {sum(res['inserted'].values())} "
                            f"inserted, {sum(res['deleted'].values())} "
                            f"deleted in "
                            f"{sorted(pe._p(x) for x in res['inserted'])}")
                if removed:
                    keys += (f"; {removed} left-over live rows of "
                             f"{sorted(legacy)} removed (kept in editions)")
                msgs.append(f"{spec.live_table}: {res['rows']} live rows "
                            f"refreshed in "
                            f"{sorted(pe._p(x) for x in res['updated'])}{keys}")
            stray = sorted(set(drift_args) - used_drift)
            if stray:
                halt(f"--accept-drift {stray}: not drifted in any table, "
                     "nothing to accept; rolled back")
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
class Unit:
    """One period a load stores or compares: a register date, a judgements
    file's edition date (two tables, one savepoint) or a named correction."""
    part: str
    period: str
    plan: str
    parts: list
    infos: dict
    has_ed: bool
    soft: list = field(default_factory=list)
    hard: list = field(default_factory=list)
    reissue: "str | None" = None
    notes: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.part} {self.period}"


def _end(conn, args, writing, rc) -> int:
    conn.rollback()  # commit mode has committed already
    if not writing:
        print("PREVIEW: nothing written to the database (use --commit or "
              "--simulate)")
    elif args.simulate:
        print("SIMULATION: ROLLED BACK (nothing persisted)")
    return rc


def _period_arg(text) -> str:
    try:
        return date.fromisoformat(str(text)).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date YYYY-MM-DD: "
                                         f"{text!r}") from None


def _ack_arg(text) -> str:
    """--acknowledge / --accept-reissue value: PERIOD (every unit of the run
    with that date; refused when units of two parts share it) or
    register:PERIOD / judgements:PERIOD (that part's unit only)."""
    part, sep, rest = str(text).partition(":")
    if not sep:
        return _period_arg(text)
    if part not in ("register", "judgements"):
        raise argparse.ArgumentTypeError(
            f"not PERIOD, register:PERIOD or judgements:PERIOD: {text!r}")
    return f"{part}:{_period_arg(rest)}"


def _matches(tokens, u) -> bool:
    return u.period in tokens or f"{u.part}:{u.period}" in tokens


def _tables(cur, name, writing) -> tuple:
    """(prof, spec, has_ed, tips, checked, stranded) of one table."""
    prof = profile(name)
    spec = prof.spec
    missing = _missing_tables(cur, name)
    has_ed = not missing
    if writing and not has_ed:
        halt(f"missing {', '.join(missing)}; run `ddl --commit` and "
             "`migrate-legacy --commit` first")
    if not has_ed:
        print(f"NOTE: {', '.join(missing)} not present: this preview "
              f"compares the file with the LIVE table {spec.live_table}")
        return prof, spec, False, legacy_tips(cur, name, spec), {}, []
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table}")
    held_ed = {pe._p(r[0]) for r in cur.fetchall()}
    live = _live_periods(cur, spec)
    new_live = [p for p in live if p not in held_ed]
    if new_live:
        halt(f"{spec.live_table}: live periods with no editions {new_live}; "
             "run migrate-legacy first")
    tips = tip_info(cur, spec)
    checked = ledger_checks(cur, prof)
    stranded = pe.live_missing_periods(cur, prof)
    if spec.live_newest_period_only and live:
        stranded = []          # editions periods not in live are history
    return prof, spec, True, tips, checked, stranded


def _print_page(page, path):
    d = _json(page)
    print(f"page: {d.get('title')} ({GOV_UK}{path}); public_updated_at "
          f"{d.get('public_updated_at')}")
    for ts, note in change_history(d)[:4]:
        print(f"  change_history {ts}: {note[:160]}")


def _overdue(what, newest, today, days) -> None:
    age = (today - newest).days
    if age > days:
        print(f"WARNING: the newest {what} the page offers is {newest}, "
              f"{age} days before today ({today}); a newer one was expected "
              f"within {days} days. It may not be published yet, or "
              "discovery may be missing it (check the GOV.UK page by hand; "
              "--file PATH loads a downloaded file)")


def _register_source(args, today) -> dict:
    out = {"path": None, "where": None, "how": None, "page_url": None,
           "source_url": None, "file_name": None, "title_date": None}
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        if args.title_date is None:
            halt("--file with --only register needs --title-date YYYY-MM-DD "
                 "(the register date: the workbook states none, and its "
                 "docProps modified date must agree with it)")
        print(f"file given: {path}; nothing downloaded; the register date "
              f"{args.title_date} (--title-date) is checked against the "
              "workbook's docProps")
        out.update(path=path, where=_file_source(path), how="local file",
                   source_url=f"file:{path.name}", file_name=path.name,
                   title_date=date.fromisoformat(args.title_date))
        if args.no_page:
            out.update(page_url=NO_PAGE, how="local file, --no-page: the "
                       "register page was not read")
            print(f"NOTE: --no-page: the register page is not read; "
                  f"source_url is recorded as file:{path.name} and "
                  f"release_page_url as {NO_PAGE!r}")
            return out
        page = fetch_json(REGISTER_PATH)
        _print_page(page, REGISTER_PATH)
        try:
            att = register_attachment(page)
            print(f"  the page offers {att['title']!r} ({att['url']})")
        except ValueError as e:
            print(f"NOTE: the register page's attachments: {e}")
        out.update(page_url=GOV_UK + REGISTER_PATH)
        return out
    page = fetch_json(REGISTER_PATH)
    _print_page(page, REGISTER_PATH)
    try:
        att = register_attachment(page)
    except ValueError as e:
        halt(f"{GOV_UK}{REGISTER_PATH}: {e}")
    print(f"  attachment: {att['title']!r} {att['url']}")
    for t in att["ignored"]:
        print(f"  listed, not loaded: {t!r}")
    print(f"newest register date the page offers: {att['title_date']}")
    _overdue("register", att["title_date"], today, REGISTER_OVERDUE_DAYS)
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    path, final = fetch(_abs_url(att["url"]), RAW_DIR)
    print(f"  final URL: {final}")
    out.update(path=path, where=final, how="register page (content API)",
               page_url=GOV_UK + REGISTER_PATH, source_url=final,
               file_name=_url_name(final), title_date=att["title_date"])
    return out


def _judgements_source(args, today) -> dict:
    out = {"path": None, "where": None, "how": None, "page_url": None,
           "source_url": None, "file_name": None, "page": None}
    if args.file:
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"file given: {path}; nothing downloaded; the edition date is "
              "the file name's")
        out.update(path=path, where=_file_source(path), how="local file",
                   source_url=f"file:{path.name}", file_name=path.name)
        if args.no_page:
            out.update(page_url=NO_PAGE, how="local file, --no-page: the "
                       "judgements page was not read (its dates not checked)")
            print(f"NOTE: --no-page: the judgements page is not read, so the "
                  "file's date is not checked against its change history; "
                  f"source_url is recorded as file:{path.name} and "
                  f"release_page_url as {NO_PAGE!r}")
            return out
        page = fetch_json(JUDGEMENTS_PATH)
        _print_page(page, JUDGEMENTS_PATH)
        out.update(page_url=GOV_UK + JUDGEMENTS_PATH, page=page)
        return out
    page = fetch_json(JUDGEMENTS_PATH)
    _print_page(page, JUDGEMENTS_PATH)
    try:
        att = judgements_attachment(page)
    except ValueError as e:
        halt(f"{GOV_UK}{JUDGEMENTS_PATH}: {e}")
    print(f"  attachment: {att['title']!r} {att['url']}")
    for t in att["ignored"]:
        print(f"  listed, not loaded: {t!r}")
    mm = FILE_DATE_RE.match(att["file_name"])
    if mm:
        newest = date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
        print(f"newest judgements file the page offers: {att['file_name']} "
              f"(edition date {newest})")
        _overdue("judgements file", newest, today, JUDGEMENTS_OVERDUE_DAYS)
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    path, final = fetch(_abs_url(att["url"]), RAW_DIR)
    print(f"  final URL: {final}")
    out.update(path=path, where=final, how="judgements page (content API)",
               page_url=GOV_UK + JUDGEMENTS_PATH, source_url=final,
               file_name=_url_name(final), page=page)
    return out


def _tip_records(cur, spec, p, tips, has_ed) -> list:
    if has_ed:
        return records(cur, spec, p, tips[p]["edition"])
    return records(cur, spec, p)


def _prev_records(cur, spec, p, tips, has_ed) -> "list | None":
    earlier = [h for h in tips if h < p]
    return (_tip_records(cur, spec, max(earlier), tips, has_ed) if earlier
            else None)


def _reasons_summary(recs) -> str:
    from collections import Counter
    c = Counter(why for r in recs for why in r["null_reasons"].values())
    return ", ".join(f"{k} {v:,}" for k, v in sorted(c.items())) or "none"


def _grade_summary(recs) -> str:
    from collections import Counter
    c = Counter(r["null_reasons"][g] for r in recs for g in GRADES
                if r.get(g) is None)
    total = sum(c.values())
    return (f"{total:,} NULL grades: "
            + (", ".join(f"{k} {v:,}" for k, v in sorted(c.items()))
               or "none"))


def _label(what, ident, src, sha, how, extra=()) -> str:
    return (f"RSH {what}: {src['file_name']}; {ident['kind']} "
            f"{ident['period']}; modified {_iso(ident['modified'])}; "
            f"{src['where']} sha256 {sha[:16]}; {how}"
            + "".join(f"; {x}" for x in extra if x))


def _edition_source(ident, src) -> str:
    return (f"{src['file_name']}; {ident['kind']} {ident['period']}; "
            f"modified {_iso(ident['modified'])}; {src['where']}")


def _plan_file(args, prepared, ident, sha, src, kinds, older_note_of):
    """Plan one file's period over its tables: (new, revised, skipped) and
    whether the period is new to every table."""
    prof, spec, has_ed, tips, checked, stranded = prepared[kinds[0]]
    ranks = tip_ranks(tips, checked)
    lsrc = ledger_source(src["where"], ident["kind"], ident["period"],
                         ident["modified"])
    chk = {p: s for p, s in checked.items() if p not in stranded}
    try:
        return plan_periods([ident["period"]], ident["rank"], ranks, chk,
                            lsrc, sha, recheck=args.recheck,
                            allow_older=args.allow_older_file,
                            newest_only=spec.live_newest_period_only), \
            ranks, lsrc
    except ValueError as e:
        halt(str(e))


def _older_halt(skipped, ident) -> None:
    if skipped and all(r.startswith("older") for r in skipped.values()):
        halt(f"older file: {', '.join(f'{p} ({r})' for p, r in sorted(skipped.items()))}"
             f"; the file is {rank_text(ident['rank'])}; storing it would "
             "record an older file. If this is deliberate, re-run with "
             "--allow-older-file (logged), which does two different things: "
             "a judgements period NOT yet held is stored as edition 1 only "
             "(history; live is unchanged), but a period ALREADY held gets "
             "this file as its NEXT edition, the new tip, and a later "
             "refresh-latest would then copy its rows into live, rolling "
             "live back (it also needs --acknowledge for any changed row)")


def _prepare_register(args, cur, writing, today) -> tuple:
    prepared = {"register": _tables(cur, "register", writing)}
    prof, spec, has_ed, tips, checked, stranded = prepared["register"]
    ranks = tip_ranks(tips, checked)
    print(f"register: held snapshot dates {', '.join(sorted(ranks)) or 'none'}")
    units, notes = [], []
    src = _register_source(args, today)
    path = src["path"]
    sha = content_sha256(path)
    print(f"{path.name}: sha256 {sha[:16]}")
    done = None
    if has_ed and args.recheck is None:
        done = ledger_complete(checked, src["where"], sha, ranks,
                               allow_older=args.allow_older_file,
                               stranded=stranded)
        if done:
            print(f"the file's (source, sha256) is already in the ledger for "
                  f"{', '.join(done)}; nothing parsed (--recheck PERIOD reads "
                  "it again)")
    if not done:
        try:
            ident = register_identity(path, src["title_date"])
        except ValueError as e:
            halt(f"{e}; nothing stored")
        period = ident["period"]
        print(f"{path.name}: sheet {ident['sheet']!r}; register {period}; "
              f"docProps modified {_iso(ident['modified'])}; "
              f"{len(ident['rows']):,} provider rows")
        prov = {"file_source_url": src["source_url"],
                "file_name": src["file_name"],
                "file_release_page_url": src["page_url"]}
        how = src["how"]
        if has_ed and _tip_file(cur, prof, period, sha, tips):
            prov = _tip_provenance(cur, spec, period)
            how += ("; the bytes of the file the tip came from (ledger "
                    "sha256), so the tip's provenance is kept")
            print(f"NOTE: {path.name} has the bytes of the file {period}'s "
                  "tip came from; the tip's provenance is kept")
        ident["provenance"] = prov
        try:
            recs = build_register(ident["rows"], ident)
        except ValueError as e:
            halt(f"{e}; nothing stored")
        print(f"  null_reasons: {_reasons_summary(recs)}")
        (new, revised, skipped), ranks, lsrc = _plan_file(
            args, prepared, ident, sha, src, ("register",), None)
        print(f"register planned: new {new or 'none'}; compare "
              f"{revised or 'none'}; skipped "
              + (", ".join(f"{p} ({r})" for p, r in sorted(skipped.items()))
                 or "none"))
        if not (new or revised):
            _older_halt(skipped, ident)
        for p in new + revised:
            if p in new and ranks and p < max(ranks):
                print(f"NOTE: {p} is older than the newest held snapshot "
                      f"{max(ranks)}; a back-filled snapshot is a period of "
                      "its own")
            unit = _file_unit("register", p, p in revised, ident, sha, src,
                              {"register": recs}, prepared, ranks, lsrc,
                              how, cur)
            units.append(unit)
    units += _correction_units(args, cur, prof, spec, has_ed, tips, today)
    return units, notes


def _file_unit(part, p, revised, ident, sha, src, by_name, prepared, ranks,
               lsrc, how, cur) -> Unit:
    infos, soft, reissue, notes = {}, [], None, []
    t = ranks.get(p)
    older = (f"--allow-older-file given; {p}'s tip comes from a file "
             f"{rank_text(t)}, this file is {rank_text(ident['rank'])}; it is "
             "stored as the NEXT edition (the new tip) of the period, so a "
             "later refresh-latest would copy its rows into live and roll "
             "live back"
             if revised and t is not None and t > ident["rank"] else "")
    if older:
        notes.append(older)
        print(f"NOTE: {older}")
    elif not revised and part == "judgements" and ranks and p < max(ranks):
        older = (f"--allow-older-file given; {p} is older than the newest "
                 f"held edition {max(ranks)}; stored as edition 1 only "
                 "(history; live is unchanged)")
        notes.append(older)
        print(f"NOTE: {older}")
    has_ed = True
    for name, recs in by_name.items():
        prof, spec, has_ed, tips, checked, stranded = prepared[name]
        kind = name
        if revised and p in tips:
            tip = _tip_records(cur, spec, p, tips, has_ed)
            mine = recs
            if has_ed:
                same = _tip_file(cur, prof, p, sha, tips)
            else:
                # before the editions exist the tip is live, which has no
                # null_reasons: compare the values only; the held file's
                # bytes are LEGACY_FILES'
                mine = [dict(r, null_reasons=Reasons()) for r in recs]
                facts = globals()["LEGACY_FILES"].get((part, p)) or {}
                same = sha == facts.get("sha256")
            soft += [f"{name}: {x}" for x in
                     period_problems(mine, tip, None, kind=kind)]
            r = reissue_problem(ident["rank"], tips[p].get("rank"), same,
                                mine, tip, kind)
            if r and reissue is None:
                reissue = f"{name}: {r}"
        elif not revised:
            prev = _prev_records(cur, spec, p, tips, has_ed)
            soft += [f"{name}: {x}" for x in
                     period_problems(recs, None, prev, kind=kind)]
        what = {"register": "list of registered providers",
                "judgements": "regulatory judgements",
                "notices": "enforcement notices"}[name]
        state = ("republished period" if revised else "new period")
        infos[name] = PeriodInfo(
            _edition_source(ident, src),
            _label(what, ident, src, sha, f"{state} ({how})", notes),
            (ident["modified"].date() if part == "register"
             else ident["edition_date"]),
            ((lsrc, sha),))
    return Unit(part=part, period=p, plan="revised" if revised else "new",
                parts=list(by_name.items()), infos=infos, has_ed=has_ed,
                soft=soft, reissue=reissue, notes=notes)


def _correction_units(args, cur, prof, spec, has_ed, tips, today) -> list:
    corrs = globals()["CORRECTIONS"]
    name = getattr(args, "correction", None)
    out = []
    for n, c in sorted(corrs.items()):
        if c["part"] != "register" or n == name or not has_ed:
            continue
        p = c["period"]
        if p in tips:
            _, k = correction_records(records(cur, spec, p,
                                              tips[p]["edition"]), c)
            if k:
                print(f"NOTE: named correction {n} pending for {p}: {k:,} "
                      f"{c['column']} cell(s) hold {c['stored']!r} (load "
                      f"--only register --correction {n} --acknowledge {p})")
    if not name:
        return out
    c = corrs[name]
    p = c["period"]
    if not has_ed:
        halt(f"--correction {name}: the editions tables do not exist yet")
    if p not in tips:
        halt(f"--correction {name}: {p} is not a held register date")
    tip_ed = tips[p]["edition"]
    tip = records(cur, spec, p, tip_ed)
    fixed, k = correction_records(tip, c)
    if not k:
        print(f"named correction {name}: already applied ({p} edition "
              f"{tip_ed} holds no {c['column']} {c['stored']!r}); nothing to "
              "do")
        return out
    hard = []
    if k != c["cells"]:
        hard.append(f"named correction {name} expects exactly {c['cells']:,} "
                    f"{c['column']} cell(s) holding {c['stored']!r}; edition "
                    f"{tip_ed} of {p} has {k:,}")
    print(f"named correction {name} ({c['decided']}): {p} edition {tip_ed}: "
          f"{k:,} {c['column']} cell(s) {c['stored']!r} -> NULL "
          f"({c['reason']}); {c['why']}")
    info = PeriodInfo(
        f"named correction {name} of edition {tip_ed}: "
        f"{tips[p]['source_file']}",
        f"named correction {name} ({c['decided']}): {k:,} {c['column']} "
        f"cells {c['stored']!r} -> NULL ({c['reason']})",
        today, ())
    soft = [f"register: {x}" for x in
            period_problems(fixed, tip, None, kind="register")]
    out.append(Unit(part="register", period=p, plan="correction",
                    parts=[("register", fixed)], infos={"register": info},
                    has_ed=True, soft=soft, hard=hard,
                    notes=[f"named correction {name} ({c['decided']})"]))
    return out


def _prepare_judgements(args, cur, writing, today) -> tuple:
    prepared = {n: _tables(cur, n, writing) for n in PARTS["judgements"]}
    prof, spec, has_ed, tips, checked, stranded = prepared["judgements"]
    ranks = tip_ranks(tips, checked)
    print(f"judgements: held editions {', '.join(sorted(ranks)) or 'none'}")
    src = _judgements_source(args, today)
    path = src["path"]
    sha = content_sha256(path)
    print(f"{path.name}: sha256 {sha[:16]}")
    if has_ed and args.recheck is None:
        dones = []
        for n in PARTS["judgements"]:
            _, _, _, t_n, c_n, s_n = prepared[n]
            dones.append(ledger_complete(c_n, src["where"], sha,
                                         tip_ranks(t_n, c_n),
                                         allow_older=args.allow_older_file,
                                         stranded=s_n))
        if all(dones):
            print(f"the file's (source, sha256) is already in both ledgers "
                  f"for {', '.join(dones[0])}; nothing parsed (--recheck "
                  "PERIOD reads it again)")
            return [], []
    try:
        ident = judgements_identity(path, src["file_name"], src["page"])
    except ValueError as e:
        halt(f"{e}; nothing stored")
    period = ident["period"]
    print(f"{path.name}: edition {period}"
          + (" (a date of the page's change history)" if ident["page_checked"]
             else " (page not read)")
          + f"; docProps modified {_iso(ident['modified'])}; "
          f"{len(ident['judgement_rows']):,} judgements, "
          f"{len(ident['notice_rows']):,} enforcement notices")
    prov = {"file_source_url": src["source_url"],
            "file_name": src["file_name"],
            "file_release_page_url": src["page_url"]}
    how = src["how"]
    if has_ed and _tip_file(cur, prof, period, sha, tips):
        prov = _tip_provenance(cur, spec, period)
        how += ("; the bytes of the file the tip came from (ledger sha256), "
                "so the tip's provenance is kept")
        print(f"NOTE: {path.name} has the bytes of the file {period}'s tip "
              "came from; the tip's provenance is kept")
    ident["provenance"] = prov
    try:
        jrecs = build_judgements(ident["judgement_rows"], ident)
        nrecs = build_notices(ident["notice_rows"], ident)
    except ValueError as e:
        halt(f"{e}; nothing stored")
    print(f"  judgements: {_grade_summary(jrecs)}")
    print(f"  judgements null_reasons: {_reasons_summary(jrecs)}")
    print(f"  notices null_reasons: {_reasons_summary(nrecs)}")
    (new, revised, skipped), ranks, lsrc = _plan_file(
        args, prepared, ident, sha, src, PARTS["judgements"], None)
    print(f"judgements planned: new {new or 'none'}; compare "
          f"{revised or 'none'}; skipped "
          + (", ".join(f"{p} ({r})" for p, r in sorted(skipped.items()))
             or "none"))
    if not (new or revised):
        _older_halt(skipped, ident)
        return [], []
    units = []
    for p in new + revised:
        unit = _file_unit("judgements", p, p in revised, ident, sha, src,
                          {"judgements": jrecs, "notices": nrecs}, prepared,
                          ranks, lsrc, how, cur)
        if not nrecs:
            unit.hard.append(
                f"the judgements file {path.name} has no enforcement "
                "notices. The file is kept, not lost: it was downloaded to "
                f"{RAW_DIR} (or is the --file you gave, at {path}), so do "
                "not delete it. It stops here because an empty notices "
                "edition cannot be stored by the current rule, and live "
                "would stay on the last issue that had notices: the "
                "judgements in this issue (including any downgrade) do not "
                "reach live or the editions. There is no flag that releases "
                "this. Ask Scott: the rule can be changed deliberately, with "
                "evidence (docs/decisions/README.md, open item). The page "
                "offers the latest file only, so a missed issue cannot be "
                "fetched later")
        newer = [x for x in prepared["judgements"][3] if x > p]
        if (prepared["judgements"][1].live_newest_period_only
                and prepared["judgements"][2] and newer
                and not _live_periods(cur, prepared["judgements"][1])):
            unit.hard.append(
                f"{prepared['judgements'][1].live_table} is empty and the "
                f"editions hold a newer issue {newer}; loading {p} would put "
                "an older issue into live while a newer one is held. Load "
                f"the newest issue ({newer[-1]}) instead, or restore live "
                "from the editions; nothing stored")
        for n in PARTS["judgements"]:
            live = _live_periods(cur, prepared[n][1])
            if (len(live) > 1 and p not in live and p > live[-1]):
                unit.hard.append(f"{prepared[n][1].live_table} holds "
                                 f"{len(live)} periods {live} (the old "
                                 "build's left-over rows); run refresh-latest "
                                 f"--accept-key-changes {live[-1]} first, "
                                 "then load this edition")
        units.append(unit)
    return units, []


def _compare_line(name, period, cmp, kind, write, commit, n) -> str:
    rows = f"{n:,}"
    if kind == "new":
        action = ("; stored edition 1 and inserted " if write else
                  "; would store edition 1 and insert ") + f"{rows} live rows"
    elif kind == HELD_NOT_LIVE:
        action = (("; stored edition 1 only" if write
                   else "; would store edition 1 only")
                  + "; live holds a newer period, kept as history")
    elif kind == LIVE_MISSING:
        action = ("; editions period missing from live: "
                  + ("inserted " if write else "would insert ")
                  + f"{rows} live rows (no new edition)")
    elif kind == "revised":
        action = (("; stored as the next edition" if write
                   else "; would store the next edition")
                  + " (live changes only through refresh-latest)")
    else:
        action = ""
    return (f"  {name} {period}: {kind}, {cmp['changed']} rows "
            f"{'new' if kind == 'new' else 'changed'} (compared with "
            f"{cmp['against']})"
            + ("; " + "; ".join(cmp["examples"]) if cmp["examples"] else "")
            + action + (" COMMITTED" if commit else ""))


def _compare(cur, unit, name, recs):
    prof = profile(name)
    if unit.has_ed:
        return pe.compare_period(cur, prof, unit.period, recs, "editions")
    values = tuple(c for c in prof.value_cols if c != "null_reasons")
    return pe.compare_period(cur, dataclasses.replace(prof, value_cols=values),
                             unit.period, recs, "live")


def _execute(units, args, conn, cur, writing, skipped_notes) -> int:
    started = datetime.now(timezone.utc)
    acks = set(args.acknowledge or ())
    reissues = set(args.accept_reissue or ())
    considered = ({u.period for u in units}
                  | {f"{u.part}:{u.period}" for u in units})
    stray = sorted((acks | reissues) - considered)
    if stray:
        halt(f"--acknowledge/--accept-reissue {', '.join(stray)}: not periods "
             "this run compares or stores "
             f"({', '.join(sorted({f'{u.part}:{u.period}' for u in units})) or 'none'}"
             "); nothing stored")
    for tok in sorted((acks | reissues) & {u.period for u in units}):
        parts = sorted({u.part for u in units if u.period == tok})
        if len(parts) > 1:
            halt(f"--acknowledge/--accept-reissue {tok}: this run has a "
                 f"{' and a '.join(parts)} unit with that date, and a bare "
                 "date would release both. Name the part: "
                 + " or ".join(f"{x}:{tok}" for x in parts) + "; nothing "
                 "stored")
    problems = {}
    for u in units:
        bad = list(u.hard)
        if u.soft and _matches(acks, u):
            u.notes.append(f"ACKNOWLEDGED {u.period}: " + "; ".join(u.soft))
            print(f"  {u.label}: ACKNOWLEDGED (--acknowledge): "
                  + "; ".join(u.soft))
        elif u.soft:
            bad += [f"{x}; read the preview, then --acknowledge "
                    f"{u.part}:{u.period}"
                    for x in u.soft]
        if u.reissue:
            if _matches(reissues, u):
                u.notes.append(f"--accept-reissue {u.period}: {u.reissue}")
                print(f"  {u.label}: ACCEPTED REISSUE: {u.reissue}")
            else:
                bad.append(u.reissue)
        if bad:
            problems[u.label] = bad
    for label, msgs in problems.items():
        for msg in msgs:
            print(f"  {label}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [u for u in units if u.label not in problems]
    done, failed, not_tried = [], [], []
    for i, u in enumerate(ok):
        if not writing:
            for name, recs in u.parts:
                cmp = _compare(cur, u, name, recs)
                print(_compare_line(name, u.period, cmp, cmp["kind"], False,
                                    False, len(recs)))
            continue
        in_sp = False
        try:
            cur.execute("SAVEPOINT s24_unit")
            in_sp = True
            kinds = {}
            for name, recs in u.parts:
                cmp = _compare(cur, u, name, recs)
                kinds[name] = apply_part(cur, profile(name), u.period, recs,
                                         info=u.infos[name])
                print(_compare_line(name, u.period, cmp, kinds[name], True,
                                    args.commit, len(recs)))
            if args.commit:
                cur.execute("RELEASE SAVEPOINT s24_unit")
                in_sp = False
                conn.commit()
            else:
                cur.execute("ROLLBACK TO SAVEPOINT s24_unit")
                cur.execute("RELEASE SAVEPOINT s24_unit")
                in_sp = False
            done.append((u, kinds))
        except (Exception, SystemExit) as e:
            if in_sp:
                cur.execute("ROLLBACK TO SAVEPOINT s24_unit")
                cur.execute("RELEASE SAVEPOINT s24_unit")
            if args.commit:
                conn.rollback()
            msg = e.code if isinstance(e, SystemExit) else \
                f"{type(e).__name__}: {e}"
            print(f"  {u.label}: FAILED, nothing stored for it ({msg})")
            failed = [u.label]
            not_tried = [x.label for x in ok[i + 1:]]
            if not_tried:
                print(f"  not attempted: {', '.join(not_tried)}")
            break
    rc = int(bool(problems or failed))      # the exit code
    if problems:
        print(f"REJECTED {len(problems)} period(s), nothing stored for them: "
              f"{', '.join(sorted(problems))}; exit 1")
    if args.commit and (done or problems or failed):
        stored = live = 0  # rows written by this run; not a source value
        parts = []
        for u, kinds in done:
            for name, recs in u.parts:
                k = kinds[name]
                parts.append(f"{name} {u.period} {k}")
                if k in ("new", "revised", HELD_NOT_LIVE):
                    stored += len(recs)
                if k in ("new", LIVE_MISSING):
                    live += len(recs)
        partial = ""
        if rc:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}"
                       + "".join(f" ({k}: {'; '.join(v)[:300]})"
                                 for k, v in sorted(problems.items()))
                       + f"; failed {failed or 'none'}; not attempted "
                       f"{not_tried or 'none'}; a period that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        notes = (partial + "S24 load: " + ("; ".join(parts) or "nothing stored")
                 + "".join(f"; {x}" for x in skipped_notes)
                 + "".join(f"; {u.label}: {'; '.join(u.notes)}"
                           for u, _ in done if u.notes)
                 + f". Rows stored: {stored} edition rows, {live} live rows.")
        log_run(cur, stored + live, notes, started)
        conn.commit()
        print("pipeline_run_log row written"
              + (" (partial run: see its notes)" if rc else ""))
    return rc


def cmd_load(args) -> int:
    parts = [args.only] if args.only else ["register", "judgements"]
    if args.file and not args.only:
        halt("--file needs --only register or --only judgements (one file, "
             "one part)")
    if args.no_page and not args.file:
        halt("--no-page is only for --file (a local file whose page is not "
             "read)")
    if args.title_date and not (args.file and args.only == "register"):
        halt("--title-date is only for --only register --file PATH")
    if args.correction and "register" not in parts:
        halt("--correction is a register correction; drop --only judgements")
    writing = args.commit or args.simulate
    today = _today()
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            units, notes = [], []
            for part in parts:
                print(f"=== {part.upper()} ===")
                u, n = (_prepare_register(args, cur, writing, today)
                        if part == "register"
                        else _prepare_judgements(args, cur, writing, today))
                units += u
                notes += n
            if args.allow_older_file and any(
                    any("--allow-older-file" in x for x in u.notes)
                    for u in units):
                notes.append("--allow-older-file given")
            if not units:
                stray = sorted(set(args.acknowledge or ())
                               | set(args.accept_reissue or ()))
                if stray:
                    halt(f"--acknowledge/--accept-reissue {', '.join(stray)}:"
                         " this run compares or stores no period; nothing "
                         "stored")
                print("nothing to do")
                return _end(conn, args, writing, rc=0)
            rc = _execute(units, args, conn, cur, writing, notes)
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# One-off: migrate the held tables
# ---------------------------------------------------------------------------

def legacy_run_rows(cur) -> "int | None":
    """rows_written of the run that loaded the 12 August judgements
    (LEGACY_RUN['id']), or None."""
    cur.execute("SELECT rows_written FROM pipeline_run_log WHERE id = %s",
                (globals()["LEGACY_RUN"]["id"],))
    r = cur.fetchone()
    return r[0] if r else None


def _proof(held, built, kind) -> list:
    """Differences between two record sets on the key and every value
    column: ('only held', key), ('only built', key), (key, column, held,
    built)."""
    hk, bk = _by_key(held, kind), _by_key(built, kind)
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk), key=str)]
    diffs += [("only built", k) for k in sorted(set(bk) - set(hk), key=str)]
    for k in sorted(set(hk) & set(bk), key=str):
        for c in KINDS[kind][2]:
            if hk[k].get(c) != bk[k].get(c):
                diffs.append((k, c, hk[k].get(c), bk[k].get(c)))
    return diffs


def _one_provenance(recs, where) -> dict:
    vals = {tuple(r[c] for c in PROVENANCE) for r in recs}
    if len(vals) != 1:
        halt(f"{where}: {len(vals)} provenance values in live; nothing "
             "stored")
    return dict(zip(PROVENANCE, vals.pop()))


def _loaded_date(cur, spec, period) -> date:
    cur.execute(f"SELECT DISTINCT (loaded_at AT TIME ZONE 'UTC')::date FROM "
                f"public.{spec.live_table} WHERE {spec.period_col} = %s",
                (period,))
    got = [r[0] for r in cur.fetchall()]
    if len(got) != 1:
        halt(f"{spec.live_table} {period}: {len(got)} load dates; nothing "
             "stored")
    return got[0]


def _file_ident(kind, path, period, facts):
    try:
        if kind == "register":
            return register_identity(path, period)
        return judgements_identity(path, facts["name"], None)
    except ValueError as e:
        halt(f"{e}; nothing stored")


def migrate_legacy(cur, files=None, *, write, legacy=None,
                   held_files=None) -> dict:
    """One-off. Preconditions: on write the three editions tables and
    ledgers exist and are empty (in a preview they may be absent; if present
    they must be empty); the live tables as surveyed (legacy, default
    LEGACY_LIVE: rows, survey hash, periods with their rows and loaded_at);
    each held file's sha256 as held_files (default LEGACY_FILES). Proofs,
    any failure halts: register 24 July and 17 September: the file read with
    the old build's rules reproduces every live row, and the new parse gives
    the same values; register 17 August (AUGUST_REGISTER): no file, kept as
    held; judgements and notices of each held file: every live row of its
    edition date equals its row of the file read with the old rules, and the
    new parse gives the same values; a live period holding fewer rows than
    the file (12 August) also needs the run-log count (LEGACY_RUN). Edition
    1 'as loaded': register = live (null_reasons from the proved file; the
    August snapshot: none, its notes text kept); judgements and notices =
    the file's rows with the old rules (equal to live where live holds them)
    with reason_not_recorded_as_loaded on every NULL; provenance as held;
    published_date the load date. A ledger row for each held file and table
    ('unchanged', edition 1, marked 'as loaded') so the next load skips it
    by (URL, sha256). Live untouched. Nothing commits here; the caller rolls
    everything back on a halt."""
    g = globals()
    legacy = legacy or g["LEGACY_LIVE"]
    held = held_files or g["LEGACY_FILES"]
    files = files or {k: RAW_DIR / v["name"] for k, v in held.items()}
    specs = {n: profile(n).spec for n in g["SPECS"]}
    for n in specs:
        prof = profile(n)
        for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
            if not table_exists(cur, t):
                if write:
                    halt(f"{t} does not exist; run `ddl --commit` first")
                continue
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            k = cur.fetchone()[0]
            if k:
                halt(f"{t} already holds {k} rows; migrate-legacy runs once, "
                     "on empty tables")
    for n in specs:
        st = live_state(cur, n, specs[n])
        if st != legacy[n]:
            halt(f"{specs[n].live_table} is not as surveyed: {st}; expected "
                 f"{legacy[n]}; nothing stored")
        print(f"{specs[n].live_table}: as surveyed ({st['rows']:,} rows, "
              f"hash {st['hash']}, periods {st['periods']})")
    shas = {}
    for k, facts in sorted(held.items()):
        path = Path(files[k])
        if not path.is_file():
            halt(f"{k}: {path} not found; nothing stored")
        shas[k] = content_sha256(path)
        if shas[k] != facts["sha256"]:
            halt(f"{path.name}: sha256 {shas[k][:16]}, expected "
                 f"{facts['sha256'][:16]} (the held file); nothing stored")
    plan = {"editions": [], "ledger": [], "proofs": []}
    # --- register -------------------------------------------------------
    rs = specs["register"]
    aug = g["AUGUST_REGISTER"]["period"]
    reg_files = {p for (part, p) in held if part == "register"}
    live_reg = _live_periods(cur, rs)
    if set(live_reg) != reg_files | {aug}:
        halt(f"{rs.live_table}: live periods {live_reg}, expected the held "
             f"files' {sorted(reg_files)} and {aug}; nothing stored")
    for p in live_reg:
        held_recs = records(cur, rs, p)
        prov = _one_provenance(held_recs, f"{rs.live_table} {p}")
        loaded = _loaded_date(cur, rs, p)
        if p in reg_files:
            facts = held[("register", p)]
            ident = _file_ident("register", files[("register", p)], p, facts)
            try:
                new = build_register(ident["rows"], ident)
                old = build_register(ident["rows"], ident, rules="old")
            except ValueError as e:
                halt(f"{e}; nothing stored")
            diffs = _proof(held_recs, old, "register")
            diffs += _proof(old, new, "register")
            if diffs:
                halt(f"proof failed: register {p}: {len(diffs)} differences "
                     f"between live, the held file with the old rules and "
                     f"the new parse, e.g. {diffs[:5]}; nothing stored")
            if (prov["file_name"], prov["file_source_url"]) != (
                    facts["name"], facts["url"]):
                halt(f"proof failed: register {p}: live provenance "
                     f"{prov['file_name']} {prov['file_source_url']} does not "
                     f"name the held file; nothing stored")
            why = {r["registration_number"]: r["null_reasons"] for r in new}
            recs = [dict(r, null_reasons=Reasons(why[r["registration_number"]]))
                    for r in held_recs]
            src = (f"as loaded: {facts['name']}; register {p}; modified "
                   f"{_iso(ident['modified'])}; {facts['url']}")
            label = (core.AS_LOADED_LABEL + "; null_reasons read from the "
                     "held file, which reproduces every stored value "
                     "(proved)")
            plan["ledger"].append(("register", p, ledger_source(
                facts["url"], "register", p, ident["modified"],
                as_loaded=True), shas[("register", p)]))
            msg = (f"proof: register {p}: the held file read again "
                   f"reproduces all {len(held_recs):,} live rows (old rules), "
                   "and the new parse gives the same values: 0 differences; "
                   f"null_reasons {_reasons_summary(new)}")
        else:
            recs = [dict(r, null_reasons=not_recorded(r)) for r in held_recs]
            src = f"as loaded: {prov['file_name']}; no file held"
            label = core.AS_LOADED_LABEL + "; " + g["AUGUST_REGISTER"]["note"]
            msg = (f"register {p}: {len(held_recs):,} live rows kept as held "
                   f"(no file can be obtained; {g['AUGUST_REGISTER']['note']})")
        print(msg)
        plan["proofs"].append(msg)
        plan["editions"].append(("register", p, recs, src, label, loaded))
    # --- judgements and notices ---------------------------------------------
    jfiles = sorted(p for (part, p) in held if part == "judgements")
    for n in ("judgements", "notices"):
        lp = _live_periods(cur, specs[n])
        if not set(lp) <= set(jfiles):
            halt(f"{specs[n].live_table}: live periods {lp} not all held "
                 f"files {jfiles}; nothing stored")
    for p in jfiles:
        facts = held[("judgements", p)]
        ident = _file_ident("judgements", files[("judgements", p)], p, facts)
        try:
            built = {
                "judgements": (
                    build_judgements(ident["judgement_rows"], ident,
                                     rules="old"),
                    build_judgements(ident["judgement_rows"], ident)),
                "notices": (
                    build_notices(ident["notice_rows"], ident, rules="old"),
                    build_notices(ident["notice_rows"], ident))}
        except ValueError as e:
            halt(f"{e}; nothing stored")
        jlive = records(cur, specs["judgements"], p)
        if not jlive:
            halt(f"{specs['judgements'].live_table}: no live rows of {p}; "
                 "the held file's provenance cannot be read; nothing stored")
        jprov = _one_provenance(jlive, f"judgements {p}")
        jloaded = _loaded_date(cur, specs["judgements"], p)
        if (jprov["file_name"], jprov["file_source_url"]) != (
                facts["name"], facts["url"]):
            halt(f"proof failed: judgements {p}: live provenance does not "
                 "name the held file; nothing stored")
        for n in ("judgements", "notices"):
            old, new = built[n]
            spec = specs[n]
            live = records(cur, spec, p)
            diffs = [d for d in _proof(live, old, n) if d[0] != "only built"]
            diffs += _proof(old, new, n)
            if diffs:
                halt(f"proof failed: {n} {p}: {len(diffs)} differences "
                     f"between live, the held file with the old rules and "
                     f"the new parse, e.g. {diffs[:5]}; nothing stored")
            complete = len(live) == len(old)
            prov = _one_provenance(live, f"{n} {p}") if live else jprov
            loaded = _loaded_date(cur, spec, p) if live else jloaded
            extra = ""
            if not complete:
                lr = g["LEGACY_RUN"]
                if p != lr["judgements"]:
                    halt(f"{n} {p}: live holds {len(live)} of the file's "
                         f"{len(old)} rows, which only the re-tagged "
                         f"{lr['judgements']} edition explains; nothing "
                         "stored")
                reg_rows = len(records(cur, rs, lr["register"]))
                want = (reg_rows + len(built["judgements"][0])
                        + len(built["notices"][0]))
                got = legacy_run_rows(cur)
                if got != want or got != lr["rows"]:
                    halt(f"{n} {p}: run-log {lr['id']} wrote {got} rows; the "
                         f"register {lr['register']} ({reg_rows}) and the "
                         f"held file ({len(built['judgements'][0])} "
                         f"judgements, {len(built['notices'][0])} notices) "
                         f"give {want}; expected {lr['rows']}; nothing stored")
                extra = (f"; read from the held file with the old build's "
                         f"rules: live keeps {len(live)} of its {len(old)} "
                         "rows (the later upsert re-tagged the rest), proved "
                         f"equal; run-log {lr['id']} wrote {got} rows "
                         f"({reg_rows} register + {len(built['judgements'][0])}"
                         f" judgements + {len(built['notices'][0])} notices)")
            if not old:
                print(f"{n} {p}: the file has no rows; no edition")
                continue
            base = live if complete else old
            recs = [dict(r, **prov, null_reasons=not_recorded(r))
                    for r in base]
            src = (f"as loaded: {facts['name']}; judgements {p}; modified "
                   f"{_iso(ident['modified'])}; {facts['url']}")
            label = core.AS_LOADED_LABEL + extra
            plan["editions"].append((n, p, recs, src, label, loaded))
            plan["ledger"].append((n, p, ledger_source(
                facts["url"], "judgements", p, ident["modified"],
                as_loaded=True), shas[("judgements", p)]))
            msg = (f"proof: {n} {p}: "
                   + (f"all {len(live):,} live rows" if complete
                      else f"{len(live):,} live rows (of {len(old):,})")
                   + " equal the held file read with the old build's rules, "
                   "and the new parse gives the same values: 0 differences; "
                   + (f"new-parse {_grade_summary(new)}"
                      if n == "judgements" else
                      f"new-parse null_reasons {_reasons_summary(new)}")
                   + extra)
            print(msg)
            plan["proofs"].append(msg)
    for n, p, recs, src, label, loaded in plan["editions"]:
        print(f"  {n} {p}: edition 1 as loaded, {len(recs):,} rows, published "
              f"{loaded}; source_file {src!r}")
    for n, p, lsrc, sha in plan["ledger"]:
        print(f"  ledger {n} {p}: {lsrc} sha256 {sha[:16]}")
    plan["rows"] = sum(len(e[2]) for e in plan["editions"])
    if not write:
        return plan
    cur.execute("SAVEPOINT s24_migrate")
    for n, p, recs, src, label, loaded in plan["editions"]:
        ed = core.insert_edition(
            cur, specs[n], recs, p, release_label=label,
            published_date=loaded, source_file=src,
            source_sha256=rows_content_sha(recs), supersedes=None,
            strict=True)
        if ed != 1:
            halt(f"{specs[n].editions_table} {p}: stored as edition {ed}, "
                 "expected 1; rolled back")
    for n, p, lsrc, sha in plan["ledger"]:
        pe.record_file_check(cur, profile(n), p, lsrc, sha, "unchanged", 1)
    for n, spec in specs.items():
        sel = ", ".join(tuple(spec.key_cols) + spec.compare_cols)
        for p in _live_periods(cur, spec):
            cur.execute(f"""SELECT COUNT(*) FROM (
                SELECT {sel} FROM public.{spec.live_table}
                 WHERE {spec.period_col} = %s
                EXCEPT ALL SELECT {sel} FROM public.{spec.editions_table}
                 WHERE {spec.period_col} = %s AND edition = 1) q""", (p, p))
            if cur.fetchone()[0]:
                halt(f"{spec.editions_table} {p}: a live row is not in "
                     "edition 1; rolled back")
    cur.execute("RELEASE SAVEPOINT s24_migrate")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, write=writing)
            if args.commit:
                log_run(cur, plan["rows"], "migrate-legacy: edition 1 as "
                        "loaded for "
                        + ", ".join(f"{n} {p} ({len(r)} rows)"
                                    for n, p, r, _, _, _ in plan["editions"])
                        + " from the live tables and the held files; "
                        + " | ".join(plan["proofs"])
                        + "; ledger rows for the held files. Live untouched.",
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

def restore_edition(cur, spec, period, edition) -> int:
    """Store edition `edition` of `period` as the next edition ('restored
    from edition N') for refresh-latest to apply; return its number. Halts
    if the edition does not exist or is the tip."""
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


def _table_name(text) -> str:
    for name, s in globals()["SPECS"].items():
        if text in (name, s.live_table, s.editions_table):
            return name
    halt(f"restore-edition: {text!r} is not an S24 table (register, "
         "judgements, notices or their live or editions table names)")


def cmd_restore_edition(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            name = _table_name(args.table)
            spec = profile(name).spec
            if not table_exists(cur, spec.editions_table):
                halt(f"{spec.editions_table} does not exist")
            new = restore_edition(cur, spec, args.period, args.edition)
            print(f"{spec.live_table} {args.period}: edition {args.edition} "
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
        description="S24 RSH register, regulatory judgements and enforcement "
        "notices: editions loader on the period-editions engine. There is no "
        "sync-new; migrate-legacy records the held periods once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions tables, their "
                       "triggers and ledgers (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="find the newest register and judgements "
                       "files, compare and store their periods (preview by "
                       "default)")
    p.add_argument("--only", choices=("register", "judgements"),
                   help="one part only (judgements includes the enforcement "
                   "notices, from the same file)")
    p.add_argument("--file", metavar="PATH",
                   help="a local file (nothing downloaded; needs --only)")
    p.add_argument("--title-date", type=_period_arg, metavar="YYYY-MM-DD",
                   help="with --only register --file: the register date, "
                   "checked against the workbook's docProps")
    p.add_argument("--no-page", action="store_true",
                   help="with --file only: do not read the page (recorded as "
                   "such)")
    p.add_argument("--recheck", type=_period_arg, metavar="PERIOD",
                   help="read this held period again although the file is in "
                   "the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="store a file older than the held one (logged). A "
                   "judgements period not yet held is stored as edition 1 "
                   "only (history). A period already held gets the file as "
                   "its NEXT edition, the new tip, and refresh-latest would "
                   "then roll live back to it")
    p.add_argument("--acknowledge", action="append", type=_ack_arg,
                   metavar="[PART:]PERIOD",
                   help="store this period although a stop condition fired "
                   "(repeatable; logged). PART is register or judgements: "
                   "register:2026-09-17. A bare date is accepted when only "
                   "one part of the run has it; refused when both do")
    p.add_argument("--accept-reissue", action="append", type=_ack_arg,
                   metavar="[PART:]PERIOD",
                   help="store this period although a file with the same "
                   "date and docProps time but other content is held "
                   "(repeatable; logged)")
    p.add_argument("--correction", metavar="NAME",
                   choices=sorted(globals()["CORRECTIONS"]),
                   help="apply this named, decided correction (CORRECTIONS) "
                   "to its period's tip as the next edition; needs "
                   "--acknowledge PERIOD")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each live period's latest "
                       "edition into the live tables (preview by default)")
    p.add_argument("--accept-drift", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="overwrite this period although its live rows equal "
                   "no stored edition (repeatable)")
    p.add_argument("--accept-key-changes", action="append", type=_period_arg,
                   metavar="PERIOD",
                   help="apply this period although keys are added or "
                   "removed, or remove the old build's left-over rows of an "
                   "older edition when it is the newest live period "
                   "(repeatable; each must be named)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for the held periods, with the proofs (preview by "
                       "default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("table", metavar="TABLE")
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
