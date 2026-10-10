"""S12 (MHCLG Exceptional Financial Support, and section 114 notices): edition
history and loader on the period-editions engine.

Two live tables keep their names, keys and columns and are the latest-edition
layer that W1 reads (sql/w1/05_la_signals.sql: efs_flag = any la_efs_support
row; s114_flag = any la_s114_notices row). This module owns their editions
tables and file-check ledgers, append-only:

    SPECS['s12_efs']   la_efs_support_editions     period financial_year,
                       key lad24cd
    SPECS['s12_s114']  la_s114_notices_editions    period financial_year,
                       key (lad24cd, notice_date)
    ledgers            <editions table>_file_checks

EFS (Exceptional Financial Support). An edition is what the year's own GOV.UK
page "Exceptional Financial Support for local authorities for <yyyy-yy>" says
about that year. Discovery: the collection "Exceptional Financial Support for
local authorities" lists one page per year (year_pages); a missing page for a
held year, two pages for a year or a retitled collection halts. Identity and
rank come from the page itself (page_identity): its title year, the table
header 'Local authority' / 'Exceptional Financial Support requests from local
authorities: <yyyy-yy>' for its own year (a header for another year halts),
and its rank, the later of public_updated_at and the newest change-history
timestamp (never the base path).

Values: amount_m is the latest figure the year's own page states for the
authority ("This was subsequently revised to" X wins over the first figure);
status is 'agreed-in-principle' when that figure says so,
'capitalisation-direction' when it carries no in-principle wording and the
page's Capitalisation directions section lists a direction for the authority
covering the year, 'withdrawn' (the request was withdrawn; amount NULL),
'grant' and 'capitalisation-extended' (the two 2020-21 qualifiers "(in the
form of grant)" and "(original capitalisation of ... was extended by ...)"),
and 'other-years-only' (the authority is listed on the year's page but its
cell states support for other years only; amount NULL); anything else halts.
hra_only is true for a row of the page's Housing Revenue Account table.
Editions-only: cell_text (the cell as read, one line per line) and
page_updated_at. The cell grammar (parse_cell) covers, and only covers, the
forms on the pages held on 2026-10-10; any other line halts naming the page,
the row and the cell. Statements on one page about another year ("£10m for
2024-25", "Note: ... revised to", the revised-profile table) are compared with
that year's own page (other_year_statements); the year's own page always wins,
and a disagreement stops that year until --acknowledge YEAR.

Names (rule 4; docs/decisions/2026-08-20-s12-efs-misattribution.md): a page
name must equal a la_boundaries.lad24nm, or an alias, or an exclusion in
scripts/s12_efs_names.json, exactly; anything else halts listing the
lad24nm values that share a whole word with it, never choosing one. Excluded
bodies (county councils, a police and crime commissioner, a mayoral combined
authority) and the Police Force table are counted and listed, not stored.

The withdrawn-only rule (WITHDRAWN_ONLY_NOT_SUPPORT, Scott 2026-10-10): an
authority whose every EFS request on the pages was withdrawn is not in the EFS
support list, so its rows are not stored (Bexley, Bournemouth, Christchurch
and Poole, North Northamptonshire). It is a named rule with its evidence:
a named authority with any request not withdrawn halts (the rule no longer
holds), and an authority not named whose every request was withdrawn halts
until it is named. Edition 1 'as loaded' keeps their rows as held; the
editions stored by load do not carry them, and refresh-latest removes them
from live only with --accept-key-changes YEAR. Withdrawn rows of an
authority with support in another year are kept (status 'withdrawn').

S.114 notices: there is no central register, so the input is the curated
register data/reference/la_s114_notices.csv, read through the manual-file
pattern (scripts/manual_input.py; read_s114): a 'register_as_at,<date>' first
line (the rank), the exact header S114_HEADER, per-row evidence columns
(evidence_url, evidence_title, checked_on). A row without evidence is reported
as unevidenced and still loaded as held; it is never invented and never
dropped. The loader never removes a notice: a held notice absent from the
register stops the year (no flag releases it); a removal is stored only when
named in S114_REMOVALS, with what was searched (a decision for Scott, made as
a listed step, never by this loader on its own). A month-only date corrected
to an exact date in the same month is a listed date correction (the key
changes, so refresh-latest needs --accept-key-changes YEAR). Rows are
attributed to the issuing authority (docs/decisions/2026-08-16-s114-
attribution-and-gate-14.md): every lad24cd is in la_boundaries unless
attribution = 'predecessor' with successor codes and a note.

History (so the wording here stays truthful): the n8n workflow "LA Financial
Stress (12)" loaded both tables on 2026-03-31 (pipeline_run_log 27, 131 rows
written; 113 EFS rows landed): seven GOV.UK pages parsed by regex, the first
amount in each cell taken, names mapped by a hard-coded dictionary (it mapped
'Haringey' to E09000013, Hammersmith and Fulham) and duplicates resolved
"last writer wins"; hra_only was never set. On 2026-08-20 the two Haringey
rows were re-attributed to E09000014 and the 2025-26 amount corrected by
direct SQL, loaded_at unchanged (decision note 2026-08-20). The S.114 rows
came from a CSV compiled in a chat session ("IfG / Wikipedia / primary
sources - compiled manually"); attribution, successor_codes and
attribution_note were added on 2026-08-16. source_check_log 63 (2026-09-04)
found Bradford 2025-26 held at 127.1 against the page's 113.0 and the two
pages disagreeing on Croydon 2025-26. migrate-legacy (one-off) records both
tables as edition 1 'as loaded' with a proof against the held files; load
then stores what the pages and the register say as the next edition.

Blanks and zeros (docs/RULES.md rule 1): a withdrawn request and a cell with
no figure for the year are amount_m NULL with the reason in status; a value
going to or from NULL against the held edition is released only by a named
entry in ACKNOWLEDGED_FLIPS (--acknowledge-flips NAME). No amount is ever
coerced to 0. A blank register cell is NULL, never ''.

Stop conditions (a stopped year is REJECTED: nothing stored, no ledger row,
exit 1; a committed run writes a run-log row, partial runs included):
    EFS revised year: an authority of the held edition missing from the page
        (PARTIAL PAGE; only the withdrawn-only rule may remove a row; nothing
        else releases it); any change of amount, status or hra_only (read the
        preview, then --acknowledge YEAR); a value to or from NULL (named
        flip); equal rank with different content (--accept-reissue YEAR); an
        older page (skipped; all older halts; --allow-older-file)
    EFS any year: another page disagreeing with the year's own page
        (--acknowledge YEAR)
    S.114: a held notice missing (never removed by the loader); a notice
        added or a new year, or a held value changed (--acknowledge YEAR);
        an older register (register_as_at earlier than the held one)
    both: the set of authorities with any row (the map's flags) may lose an
        authority only under the withdrawn-only rule or S114_REMOVALS

W1 input: a new year's live rows take loaded_at = now(); a revision reaches
live only through refresh-latest, which copies the edition's loaded_at, so run
refresh-latest in the same session before W1.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
years.
    python scripts/s12_financial_stress_editions.py ddl [--commit | --simulate]
    python scripts/s12_financial_stress_editions.py status
    python scripts/s12_financial_stress_editions.py load [--only efs|s114]
        [--year YYYY-YY] [--page-file PATH ... [--no-page]]
        [--s114-file PATH] [--recheck YYYY-YY] [--allow-older-file]
        [--acknowledge YYYY-YY ...] [--accept-reissue YYYY-YY ...]
        [--acknowledge-flips NAME] [--commit | --simulate]
        # pages are saved to data/raw/s12_efs/ (also in a preview; a preview
        # writes nothing to the database)
    python scripts/s12_financial_stress_editions.py refresh-latest
        [--accept-drift YYYY-YY] [--accept-key-changes YYYY-YY]
        [--commit | --simulate]
    python scripts/s12_financial_stress_editions.py migrate-legacy
        [--commit | --simulate]
    python scripts/s12_financial_stress_editions.py restore-edition
        s12_efs|s12_s114 YYYY-YY N [--commit | --simulate]
"""
import argparse
import csv
import dataclasses
import hashlib
import html
import io
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import manual_input  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s12_financial_stress_editions"
RUN_SOURCE = "12"
LIVE_MISSING = pe.LIVE_MISSING

API = "https://www.gov.uk/api/content"
GOV_UK = "https://www.gov.uk"
COLLECTION_PATH = ("/government/collections/exceptional-financial-support-"
                   "for-local-authorities")
COLLECTION_TITLE = "Exceptional Financial Support for local authorities"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s12_efs"               # git-ignored
NAMES_FILE = Path(__file__).resolve().parent / "s12_efs_names.json"
S114_FILE = REPO / "data" / "reference" / "la_s114_notices.csv"
PAGE_ROOTS = manual_input.ROOTS
S114_ROOTS = manual_input.ROOTS
USER_AGENT = ("ucws-pipeline S12 loader (read-only download of the public "
              "MHCLG Exceptional Financial Support pages)")

LABEL = r"([0-9]{4}-[0-9]{2})"
PAGE_TITLE_RE = re.compile(r"Exceptional Financial Support for local "
                           rf"authorities for {LABEL}")
HEADER_FIRST = "Local authority"
HEADER_SECOND = "Exceptional Financial Support requests from local authorities: "
HEADER_RE = re.compile(re.escape(HEADER_SECOND) + LABEL)

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-10:
# la_efs_support lad24cd varchar NOT NULL, financial_year varchar NOT NULL,
# amount_m numeric(10,3), status text, hra_only boolean DEFAULT false, source
# text, loaded_at timestamptz DEFAULT now(); PK (lad24cd, financial_year).
# la_s114_notices lad24cd varchar NOT NULL, notice_date date NOT NULL,
# financial_year varchar, reason text, date_confirmed text, source text,
# loaded_at timestamptz DEFAULT now(), attribution text, successor_codes
# text[], attribution_note text; PK (lad24cd, notice_date); CHECK
# la_s114_notices_attribution_chk. No FK on either.)
# ---------------------------------------------------------------------------

STATUSES = ("agreed-in-principle", "capitalisation-direction", "withdrawn",
            "grant", "capitalisation-extended", "other-years-only")
NULL_STATUSES = ("withdrawn", "other-years-only")    # amount_m NULL, why
QUAL_STATUS = {"in-principle": "agreed-in-principle", "grant": "grant",
               "extended": "capitalisation-extended"}
EFS_VALUE_TYPES = (("amount_m", "numeric(10,3)"), ("status", "text NOT NULL"),
                   ("hra_only", "boolean NOT NULL"))
EFS_EXTRA_TYPES = (("cell_text", "text"), ("page_updated_at", "timestamptz"))
EFS_VALUES = tuple(c for c, _ in EFS_VALUE_TYPES)
EFS_COMPARED = EFS_VALUES + ("cell_text",)

S114_HEADER = ("la_name", "lad24cd", "notice_date", "financial_year",
               "reason", "date_confirmed", "attribution", "successor_codes",
               "attribution_note", "evidence_url", "evidence_title",
               "checked_on")
S114_EVIDENCE = ("evidence_url", "evidence_title", "checked_on")
DATE_CONFIRMED = ("exact", "approximate - month only confirmed")
ATTRIBUTIONS = ("direct", "predecessor")
S114_VALUE_TYPES = (("reason", "text"), ("date_confirmed", "text NOT NULL"),
                    ("attribution", "text NOT NULL"),
                    ("successor_codes", "text[]"),
                    ("attribution_note", "text"))
S114_EXTRA_TYPES = (("evidence_url", "text"), ("evidence_title", "text"),
                    ("checked_on", "date"))
S114_VALUES = tuple(c for c, _ in S114_VALUE_TYPES)
S114_COMPARED = S114_VALUES + tuple(c for c, _ in S114_EXTRA_TYPES)
S114_KEY = ("lad24cd", "notice_date")
CODE_RE = re.compile(r"E[0-9]{8}")

# ---------------------------------------------------------------------------
# The withdrawn-only rule (Scott, 2026-10-10)
# ---------------------------------------------------------------------------

WITHDRAWN = "Council provided with in-principle support but withdrew its request"
WITHDRAWN_ONLY_NOT_SUPPORT = {
    "name": "WITHDRAWN_ONLY_NOT_SUPPORT",
    "decided": ("Scott, 2026-10-10: the EFS flag is dropped for Bexley, "
                "Bournemouth, Christchurch and Poole and North "
                "Northamptonshire because every EFS request they made was "
                "withdrawn; a withdrawn request is not EFS support. Stored as "
                "an additive edition; edition 1 keeps the rows as held."),
    "codes": {
        "E09000004": ("Bexley: the 2020-21 and 2021-22 pages say '" + WITHDRAWN
                      + "'; Bexley is on no other year page. The 2020-21 "
                      "page also lists 'Bexley capitalisation direction "
                      "2020-21' and an amended one ('This capitalisation "
                      "direction was amended in December 2024'; change note "
                      "of 13 March 2025 'Added varied directions for: Bexley, "
                      "Eastbourne, Luton, Nottingham, Peterborough and "
                      "Wirral'); its table row still says the request was "
                      "withdrawn (pages read 2026-10-10)."),
        "E06000058": ("Bournemouth, Christchurch and Poole: the 2022-23 page "
                      "says '" + WITHDRAWN + "'; it is on no other year page "
                      "and has no capitalisation direction listed (pages read "
                      "2026-10-10)."),
        "E06000061": ("North Northamptonshire: the 2024-25 page says '"
                      + WITHDRAWN + "'; it is on no other year page and has "
                      "no capitalisation direction listed (pages read "
                      "2026-10-10)."),
    },
}

# Values going to or from NULL against the held edition (rule 1.10), released
# only by name: {NAME: {decided, why, periods: {year: {lad24cd: (held amount
# as text or None, new amount as text or None)}}}}. Each was found read-only on
# 2026-10-10 by comparing the held pages, read by this loader, with live.
ACKNOWLEDGED_FLIPS = {
    "s12-efs-own-year-2026-10": {
        "decided": ("the S12 rule of the 2026-10-10 design (amount_m is the "
                    "latest figure the year's own page states; a cell with "
                    "no figure for the year is NULL with status "
                    "other-years-only); listed for Scott"),
        "why": ("the n8n load stored the first amount in each cell against "
                "the page's year. The 2024-25 page gives Plymouth only "
                "'£72.0m for 2025-26' and the 2025-26 page gives Shropshire "
                "only '£26.9m for 2024-25', so neither page states a figure "
                "for its own year. The rows are kept (efs_flag unchanged); "
                "the amounts become NULL with status other-years-only"),
        "periods": {"2024-25": {"E06000026": ("72.000", None)},
                    "2025-26": {"E06000051": ("26.900", None)}},
    },
}

# Named S.114 removals (none). A removal changes s114_flag, so it is Scott's
# decision, made as a listed step with what was searched:
# {(lad24cd, 'yyyy-mm-dd'): {'decided': ..., 'searched': ...}}.
S114_REMOVALS = {}

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-10
# ---------------------------------------------------------------------------

# (rows, survey hash, distinct loaded_at, rows per year); the survey hash is
# md5 of the columns but loaded_at, each coalesce(col::text,'~'), '|'-joined,
# rows ordered by financial_year, lad24cd (EFS) and lad24cd, notice_date
# (S.114), joined by LF (live_state).
LEGACY_EFS = (113, "2e581ba40c423195" "d40e0ab76345e382", 1,
              (("2020-21", 9), ("2021-22", 8), ("2022-23", 5),
               ("2023-24", 8), ("2024-25", 19), ("2025-26", 29),
               ("2026-27", 35)))
LEGACY_S114 = (15, "0fad67960e1fce41" "19ef17ec7c373d8c", 1,
               (("2000-01", 2), ("2017-18", 1), ("2018-19", 1),
                ("2020-21", 1), ("2021-22", 4), ("2022-23", 2),
                ("2023-24", 3), ("2024-25", 1)))
# the seven year pages as content-API JSON, read 2026-10-10 (sha256 of the
# file bytes, split so the credential scan does not flag it)
LEGACY_PAGES = {
    y: {"path": RAW_DIR / f"efs_{y}_content_2026-10-10.json", "sha256": s}
    for y, s in (
        ("2020-21", "984601842271850f436fca2a97e54075"
                    "c0d6e53bfdc8d4b8b2a7a4b244121117"),
        ("2021-22", "217ee7adb2cdf080d2b5856aafe612f3"
                    "e0f11e3d9f049e10f521ed6f3116826d"),
        ("2022-23", "88f00f5972883b389c3639b99829c1de"
                    "17e8c7f313ae7fe8783275cad91d11fa"),
        ("2023-24", "afbc78479af42762584603f3bcc64592"
                    "027de9c38b802681d9efd007527d8786"),
        ("2024-25", "9f4f7c6691a57adeef9f6538668db1ea"
                    "f53670070363d20630fbc6133fcd7858"),
        ("2025-26", "12a991b95d0e1428084cf162a70c5d53"
                    "40e72c6a2bd2d7bb95ec045ad386d20c"),
        ("2026-27", "c7adeba84df71f261a2c2138abd402e3"
                    "1d6ad663b3f01c77094e34373dc71b5c"))}
# the curated S.114 list the n8n load read (six columns, no register_as_at)
LEGACY_S114_FILE = {"path": S114_FILE,
                    "sha256": "9ec96e62854062f497d0d470832a8d2d"
                              "aa757fb94399be0b642c6c139cb0857e"}
LEGACY_S114_HEADER = ("la_name", "lad24cd", "notice_date", "financial_year",
                      "reason", "date_confirmed")
N8N_S114_SOURCE = "IfG / Wikipedia / primary sources — compiled manually"
EFS_AS_LOADED_SOURCE = ("as loaded: n8n run-log 27 (2026-03-31), the GOV.UK "
                        "page parsed by regex (first amount in each cell, "
                        "a hard-coded name dictionary); the Haringey rows "
                        "re-attributed by direct SQL on 2026-08-20 with "
                        "loaded_at unchanged; no copy of the page was kept")
S114_AS_LOADED_SOURCE = ("as loaded: n8n run-log 27 (2026-03-31) from the "
                         "curated list {} (sha256 {}); attribution columns "
                         "added 2026-08-16")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    return json.loads(x) if isinstance(x, (str, bytes)) else (x or {})


def _norm(s) -> str:
    """Entities decoded, every run of whitespace (no-break and em spaces
    included) one space, stripped."""
    return " ".join(html.unescape(str("" if s is None else s)).split())


def _strip_tags(s) -> str:
    return _norm(re.sub(r"<[^>]+>", " ", str(s or "")))


def label_ok(label) -> str:
    """The label if it is yyyy-yy with consecutive years, else ValueError."""
    mm = re.fullmatch(LABEL, str(label or ""))
    if not mm or (int(label[:4]) + 1) % 100 != int(label[5:]):
        raise ValueError(f"{label!r} is not a financial year yyyy-yy")
    return label


def fy_of(d: date) -> str:
    """The financial year (April to March) a date falls in."""
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def _when(text) -> datetime:
    """An ISO timestamp as an aware UTC datetime."""
    t = str(text or "").strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    d = datetime.fromisoformat(t)
    if d.tzinfo is None:
        raise ValueError(f"timestamp without a time zone: {text!r}")
    return d.astimezone(timezone.utc)


def _amount(text, where) -> Decimal:
    """'12.345' as Decimal at the column's 3 dp; more decimals halt."""
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{where}: {text!r} is not an amount") from None
    q = d.quantize(Decimal("0.001"))
    if q != d:
        raise ValueError(f"{where}: £{text}m has more than 3 decimal places "
                         "(amount_m is numeric(10,3))")
    return q


def _amount_text(v) -> str:
    return "" if v is None else f"{Decimal(v).quantize(Decimal('0.001'))}"


def content_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page_sha(page_json) -> str:
    """sha256 of the page JSON in canonical form (sorted keys, no spaces), so
    the same content read from the content API or from a saved file has one
    sha256 (the ledger's)."""
    text = json.dumps(_json(page_json), sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The cell grammar (parse_cell)
# ---------------------------------------------------------------------------

A = r"£([0-9]+(?:\.[0-9]+)?)m"
MONTH = (r"(?:January|February|March|April|May|June|July|August|September|"
         r"October|November|December) [0-9]{4}")
# figure forms of one line: (regex, qualifier, the group holding a year or
# None); every amount is group 1
_FIGURES = (
    (re.compile(rf"{A}"), None, None),
    (re.compile(rf"{A} \(support agreed in-principle\)"), "in-principle", None),
    (re.compile(rf"{A} \(support agreed in-principle for {LABEL}\)"),
     "in-principle", 2),
    (re.compile(rf"{A} \(in the form of grant\)"), "grant", None),
    (re.compile(rf"{A} \(original capitalisation of {A} in {LABEL} was "
                rf"extended by {A}\)"), "extended", None),
    (re.compile(rf"{A} \(covering {LABEL} to {LABEL}\)"), "covering", None),
    (re.compile(rf"{A} \(from {A} - support agreed in-principle\)"),
     "in-principle", None),
    (re.compile(rf"{A} for {LABEL}"), None, 2),
    (re.compile(rf"{A} agreed in-principle for {LABEL}"), "in-principle", 2),
    (re.compile(rf"This was subsequently revised to:? {A} \(support agreed "
                r"in-principle\)"), "in-principle", None),
)
_REPROFILED = re.compile(
    rf"{A} agreed in-principle for {LABEL}(?: \({MONTH}\))?, then "
    rf"subsequently reprofiled to {A}( agreed in-principle)? for {LABEL}"
    rf"(?: \({MONTH}\))?")
_MARKERS = ("This was subsequently revised to:", "This was reprofiled to:")
_COVERING = re.compile(rf"{A}( agreed in-principle)? covering:")
_COVER_LINE = re.compile(rf"{LABEL}: {A}")
_NOTE = re.compile(r"Note: For support agreed in[- ]principle for (.+)")
_NOTE_SPLIT = re.compile(r", and for support agreed in-principle for |"
                         r", and for |, for ")
_NOTE_CLAUSE = re.compile(rf"{LABEL}, this has been revised to {A} \(from "
                          rf"{A}(?: agreed in {MONTH})?\)")
_PROVISIONAL = re.compile(rf"Note: Provisionally (?:includes|incudes) revised "
                          rf"support agreed in {LABEL}, subject to final "
                          r"confirmation")


@dataclass(frozen=True)
class Statement:
    """One figure, total or note of a cell. year None: the page's own year
    (no year written); amount None for a note; qual 'in-principle', 'grant',
    'extended', 'covering' or None; kind 'figure', 'total' or 'note'."""
    year: "str | None"
    amount: "Decimal | None"
    qual: "str | None"
    kind: str
    text: str


@dataclass(frozen=True)
class Cell:
    lines: tuple
    statements: tuple
    withdrawn: bool

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def figure(self, year) -> "Statement | None":
        """The last figure the cell states for `year` (unqualified figures
        are the page's own year), or None."""
        hits = [s for s in self.statements if s.kind == "figure"
                and (s.year is None or s.year == year)]
        return hits[-1] if hits else None

    def others(self, year) -> list:
        """The figures the cell states for other years, in order."""
        return [s for s in self.statements if s.kind == "figure"
                and s.year is not None and s.year != year]


def _cell_lines(text) -> list:
    t = str(text or "")
    if "<" in t:
        tags = {m.group(1).lower() for m in re.finditer(r"<\s*/?\s*([a-zA-Z]+)",
                                                         t)}
        if tags - {"br"}:
            raise ValueError(f"tags other than <br> in the cell: "
                             f"{sorted(tags - {'br'})}")
        parts = re.split(r"<br\s*/?>", t, flags=re.I)
    else:
        parts = t.split("\n")
    return [x for x in (_norm(p) for p in parts) if x]


def _figure_line(line, where) -> "list | None":
    """The statements of one figure line, or None if it is not one."""
    mm = _REPROFILED.fullmatch(line)
    if mm:
        return [Statement(mm.group(2), _amount(mm.group(1), where),
                          "in-principle", "figure", line),
                Statement(mm.group(5), _amount(mm.group(3), where),
                          "in-principle" if mm.group(4) else None, "figure",
                          line)]
    for rx, qual, yg in _FIGURES:
        mm = rx.fullmatch(line)
        if mm:
            year = label_ok(mm.group(yg)) if yg else None
            return [Statement(year, _amount(mm.group(1), where), qual,
                              "figure", line)]
    return None


def parse_cell(text, where="") -> Cell:
    """The statements of one EFS table cell (its HTML with <br> line breaks,
    or a stored cell_text with one line per line). The grammar covers, and
    only covers, the forms on the pages held on 2026-10-10 (module
    docstring); any other line, a marker with no figure after it, or a
    'covering:' total with no year lines raises ValueError naming `where`
    and the line."""
    pre = f"{where}: " if where else ""
    try:
        lines = _cell_lines(text)
    except ValueError as e:
        raise ValueError(f"{pre}{e}") from None
    if not lines:
        raise ValueError(f"{pre}an empty cell")
    if any("withdr" in x for x in lines):
        if len(lines) == 1 and "£" not in lines[0]:
            return Cell(tuple(lines), (), True)
        raise ValueError(f"{pre}a cell mentioning a withdrawal with other "
                         f"text is not a form this loader knows: "
                         f"{' | '.join(lines)!r}")
    # cover: None outside a 'covering:' list, else its qualifier ('' none)
    out, pending, cover, cover_n = [], None, None, 0
    for line in lines:
        bad = ValueError(f"{pre}cell line {line!r} is not a form this loader "
                         f"knows (cell: {' | '.join(lines)!r})")
        if cover is not None:
            mm = _COVER_LINE.fullmatch(line)
            if mm:
                out.append(Statement(label_ok(mm.group(1)),
                                     _amount(mm.group(2), where),
                                     cover or None, "figure", line))
                cover_n += 1
                continue
            if not cover_n:
                raise bad
            cover = None
        if line in _MARKERS:
            if pending:
                raise bad
            pending = line
            continue
        figs = _figure_line(line, where)
        if figs is not None:
            out += figs
            pending = None
            continue
        if pending:
            raise bad
        mm = _COVERING.fullmatch(line)
        if mm:
            out.append(Statement(None, _amount(mm.group(1), where),
                                 "in-principle" if mm.group(2) else None,
                                 "total", line))
            cover = "in-principle" if mm.group(2) else ""
            cover_n = 0
            continue
        mm = _NOTE.fullmatch(line)
        if mm:
            for clause in _NOTE_SPLIT.split(mm.group(1)):
                cm = _NOTE_CLAUSE.fullmatch(clause)
                if not cm:
                    raise bad
                out.append(Statement(label_ok(cm.group(1)),
                                     _amount(cm.group(2), where),
                                     "in-principle", "figure", line))
            continue
        mm = _PROVISIONAL.fullmatch(line)
        if mm:
            out.append(Statement(label_ok(mm.group(1)), None, None, "note",
                                 line))
            continue
        raise bad
    if pending:
        raise ValueError(f"{pre}{pending!r} with no figure after it (cell: "
                         f"{' | '.join(lines)!r})")
    if cover is not None and not cover_n:
        raise ValueError(f"{pre}a 'covering:' total with no year lines after "
                         f"it (cell: {' | '.join(lines)!r})")
    return Cell(tuple(lines), tuple(out), False)


# ---------------------------------------------------------------------------
# Tables, headings, capitalisation directions, identity, discovery
# ---------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"<h([23])\b[^>]*>(.*?)</h\1>|<table\b[^>]*>(.*?)</table>",
                       re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<t([hd])\b[^>]*>(.*?)</t\1>", re.S | re.I)
_MAIN_HEADING = re.compile(re.escape(HEADER_SECOND) + LABEL)
_SUPPORT_HEADING = re.compile(rf"Capitalisation support for {LABEL}")
_OTHER_YEARS_HEADING = re.compile(r"(.+?) [–-] Revised Exceptional "
                                  r"Financial Support for previous financial "
                                  r"years")


def _blocks(body) -> list:
    """[(heading or None, header cells or None, [(name, cell html)])] of
    every table in the body, with the nearest <h2>/<h3> before it."""
    out, heading = [], None
    for mm in _BLOCK_RE.finditer(str(body or "")):
        if mm.group(2) is not None:
            heading = _strip_tags(mm.group(2))
            continue
        header, rows = None, []
        for tr in _ROW_RE.findall(mm.group(3)):
            cells = _CELL_RE.findall(tr)
            if cells and all(k.lower() == "h" for k, _ in cells):
                header = [_strip_tags(c) for _, c in cells]
                continue
            if len(cells) != 2 or any(k.lower() != "d" for k, _ in cells):
                raise ValueError(f"a table row under {heading or 'no heading'!r} "
                                 f"has {len(cells)} cells, not 2: "
                                 f"{_strip_tags(tr)!r}")
            rows.append((_strip_tags(cells[0][1]), cells[1][1]))
        out.append((heading, header, rows))
    return out


def table_kind(heading) -> tuple:
    """(kind, detail) of a table's heading: ('main', None or its year),
    ('hra', None), ('police', None), ('capitalisation-support', year),
    ('other-years', authority name). Any other heading raises ValueError."""
    if heading is None:
        return "main", None
    h = _norm(heading)
    mm = _MAIN_HEADING.fullmatch(h)
    if mm:
        return "main", mm.group(1)
    if h == "Housing Revenue Account":
        return "hra", None
    if h == "Police Force":
        return "police", None
    mm = _SUPPORT_HEADING.fullmatch(h)
    if mm:
        return "capitalisation-support", mm.group(1)
    mm = _OTHER_YEARS_HEADING.fullmatch(h)
    if mm:
        return "other-years", mm.group(1)
    raise ValueError(f"a table under the heading {h!r}, which is not one this "
                     "loader knows (main table, Housing Revenue Account, "
                     "Police Force, Capitalisation support for <year>, "
                     "<authority> - Revised Exceptional Financial Support for "
                     "previous financial years)")


def tables(body) -> list:
    """[(heading, [(name, cell html)])] of every table in the body; a table
    under an unknown heading raises ValueError (table_kind)."""
    out = []
    for heading, _, rows in _blocks(body):
        table_kind(heading)
        out.append((heading, rows))
    return out


_DIRECTION_RE = re.compile(rf"(.+?) [Cc]apitalisation [Dd]irection {LABEL}"
                           rf"(?: (and|to) {LABEL})?")
# the one other link shape under the heading on the held pages (2020-21:
# 'Redcar and Cleveland grant determination 2020-21'); recognised, never
# counted as a direction
_GRANT_RE = re.compile(rf"(.+?) grant determination {LABEL}")


def direction_titles(body) -> list:
    """[(name, first year, 'and'|'to'|None, second year, kind)] of every
    link under the page's 'Capitalisation directions' heading, kind
    'direction' or 'grant determination'; a link of another shape raises
    ValueError."""
    b = str(body or "")
    mm = re.search(r"<h[23]\b[^>]*>\s*Capitalisation directions\s*</h[23]>", b,
                   re.I)
    if not mm:
        return []
    rest = b[mm.end():]
    nxt = re.search(r"<h[23]\b", rest, re.I)
    section = rest[:nxt.start()] if nxt else rest
    out = []
    for link in re.findall(r"<a\b[^>]*>(.*?)</a>", section, re.S | re.I):
        t = _strip_tags(link)
        dm = _DIRECTION_RE.fullmatch(t)
        gm = _GRANT_RE.fullmatch(t)
        if dm:
            out.append((dm.group(1), label_ok(dm.group(2)), dm.group(3),
                        label_ok(dm.group(4)) if dm.group(4) else None,
                        "direction"))
        elif gm:
            out.append((gm.group(1), label_ok(gm.group(2)), None, None,
                        "grant determination"))
        else:
            raise ValueError(f"a link under 'Capitalisation directions' is not "
                             f"'<name> capitalisation direction <yyyy-yy>' "
                             f"(or '<name> grant determination <yyyy-yy>'): "
                             f"{t!r}")
    return out


def section_names(body) -> set:
    """Every name under the 'Capitalisation directions' heading (each must
    match exactly, like a table row)."""
    return {t[0] for t in direction_titles(body)}


def capitalisation_directions(body, year) -> set:
    """The names listed under the page's 'Capitalisation directions' heading
    with a direction covering `year` ('<y>', '<y1> and <y2>', '<y1> to
    <y2>')."""
    out = set()
    for name, y1, op, y2, kind in direction_titles(body):
        if kind != "direction":
            continue
        if year == y1 or (op == "and" and year == y2) or \
                (op == "to" and y1 <= year <= y2):
            out.add(name)
    return out


def change_history(page_json) -> list:
    d = _json(page_json)
    ch = ((d.get("details") or {}).get("change_history")) or []
    return [(_when(c.get("public_timestamp")), _norm(c.get("note"))) for c in ch]


def page_identity(page_json) -> dict:
    """{year, title, base_path, public_updated_at, change_history, rank} of a
    year page, from the page itself: the title 'Exceptional Financial Support
    for local authorities for <yyyy-yy>', at least one table headed 'Local
    authority' / 'Exceptional Financial Support requests from local
    authorities: <its year>' and no table header for another year. rank is
    the later of public_updated_at and the newest change-history timestamp
    (UTC); the base path is never read for it. ValueError otherwise."""
    d = _json(page_json)
    title = _norm(d.get("title"))
    mm = PAGE_TITLE_RE.fullmatch(title)
    if not mm:
        raise ValueError(f"the page is titled {title!r}, not 'Exceptional "
                         "Financial Support for local authorities for "
                         "<yyyy-yy>'")
    year = label_ok(mm.group(1))
    body = (d.get("details") or {}).get("body")
    own = 0
    for heading, header, _ in _blocks(body):
        if header is None:
            continue
        hm = HEADER_RE.fullmatch(header[1]) if len(header) == 2 else None
        if not hm or header[0] != HEADER_FIRST:
            raise ValueError(f"the {year} page has a table headed {header!r}, "
                             f"not ['{HEADER_FIRST}', '{HEADER_SECOND}"
                             f"{year}']")
        if hm.group(1) != year:
            raise ValueError(f"the {year} page has a table headed for "
                             f"{hm.group(1)} (under {heading!r})")
        own += 1
    if not own:
        raise ValueError(f"the {year} page has no table headed "
                         f"'{HEADER_FIRST}' / '{HEADER_SECOND}{year}'")
    updated = _when(d.get("public_updated_at"))
    hist = change_history(d)
    rank = max([updated] + [t for t, _ in hist])
    return {"year": year, "title": title, "base_path": d.get("base_path"),
            "public_updated_at": updated, "change_history": hist,
            "rank": rank}


class YearPages(dict):
    """{year: base_path}; .others the titles of documents of another shape,
    .updated {year: the collection's public_updated_at for the page}."""
    others: list
    updated: dict


def year_pages(collection_json, held=()) -> YearPages:
    """The collection's year pages: documents titled exactly 'Exceptional
    Financial Support for local authorities for <yyyy-yy>'. ValueError when
    the collection is retitled, two documents claim a year, or a held year
    has no page (listing the titles seen)."""
    d = _json(collection_json)
    t = _norm(d.get("title"))
    if t != COLLECTION_TITLE:
        raise ValueError(f"the collection is titled {t!r}, not "
                         f"{COLLECTION_TITLE!r}; the page has changed")
    docs = list(((d.get("links") or {}).get("documents")) or [])
    out = YearPages()
    out.others, out.updated = [], {}
    titles = [_norm(x.get("title")) for x in docs]
    for doc, title in zip(docs, titles):
        mm = PAGE_TITLE_RE.fullmatch(title)
        if not mm or not doc.get("base_path"):
            out.others.append(title)
            continue
        y = label_ok(mm.group(1))
        if y in out:
            raise ValueError(f"two documents claim {y}: {title!r}; refusing "
                             f"to choose one; titles seen: {titles}")
        out[y] = doc["base_path"]
        out.updated[y] = doc.get("public_updated_at")
    missing = sorted(set(held) - set(out))
    if missing:
        raise ValueError(f"no page in the collection for held year(s) "
                         f"{missing}; titles seen: {titles}")
    return out


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NameBook:
    """la_boundaries {lad24nm: lad24cd} and the aliases {page name:
    lad24cd} of scripts/s12_efs_names.json."""
    boundaries: dict
    aliases: dict


def load_names(path=None) -> tuple:
    """(aliases {name: lad24cd}, exclusions {name: info}) from the committed
    names file. ValueError on a malformed entry, an entry with no page it was
    seen on, or a name that is both."""
    raw = json.loads(Path(path or globals()["NAMES_FILE"]).read_text(
        encoding="utf-8"))
    aliases, excl = {}, {}
    for name, a in (raw.get("aliases") or {}).items():
        if not CODE_RE.fullmatch(str(a.get("lad24cd") or "")) or \
                not a.get("seen"):
            raise ValueError(f"s12_efs_names.json alias {name!r}: needs a "
                             "lad24cd and the page(s) it was seen on")
        aliases[name] = a["lad24cd"]
    for name, x in (raw.get("exclusions") or {}).items():
        if not x.get("seen") or not x.get("kind"):
            raise ValueError(f"s12_efs_names.json exclusion {name!r}: needs a "
                             "kind and the page(s) it was seen on")
        excl[name] = x
    both = sorted(set(aliases) & set(excl))
    if both:
        raise ValueError(f"s12_efs_names.json: {both} are both aliases and "
                         "exclusions")
    return aliases, excl


def alias_predecessors(path=None) -> dict:
    """{alias name: (predecessor code, lad24cd)} of aliases naming an
    abolished authority (checked against la_code_lookup by load)."""
    raw = json.loads(Path(path or globals()["NAMES_FILE"]).read_text(
        encoding="utf-8"))
    return {n: (a["predecessor_code"], a["lad24cd"])
            for n, a in (raw.get("aliases") or {}).items()
            if a.get("predecessor_code")}


def _words(name) -> set:
    return {w for w in re.split(r"[^\w&'-]+", str(name)) if len(w) >= 4}


def match_name(name, boundaries, aliases, exclusions) -> "str | None":
    """The lad24cd of a page name by exact equality with a lad24nm or an
    alias; None for an exclusion (skipped and counted by the caller).
    Anything else raises ValueError listing the lad24nm values sharing a
    whole word with the name, never choosing one."""
    if name in exclusions:
        return None
    hit = boundaries.get(name)
    alias = aliases.get(name)
    if hit and alias and hit != alias:
        raise ValueError(f"{name!r} is a lad24nm ({hit}) and an alias for "
                         f"{alias}; fix s12_efs_names.json")
    if hit or alias:
        return hit or alias
    w = _words(name)
    cands = sorted(n for n in boundaries if w & _words(n))
    raise ValueError(f"{name!r} is not exactly a la_boundaries.lad24nm, an "
                     f"alias or an exclusion in {NAMES_FILE.name}; lad24nm "
                     f"values sharing a whole word: {cands or 'none'} (listed, "
                     "never chosen: add an exact alias or exclusion with the "
                     "page it was seen on)")


def resolve_directions(names, book, exclusions) -> set:
    """The lad24cd of each capitalisation-direction name (exclusions
    skipped); an unknown name raises ValueError (match_name)."""
    out = set()
    for n in sorted(names):
        c = match_name(n, book.boundaries, book.aliases, exclusions)
        if c:
            out.add(c)
    return out


# ---------------------------------------------------------------------------
# Records, statements about other years, the withdrawn-only rule
# ---------------------------------------------------------------------------

class Records(list):
    """A year page's records; .excluded [(name, table kind, cell text)] and
    .statements [{source_year, name, lad24cd, year, amount, text}] (figures
    the page states for other years)."""
    excluded: list
    statements: list


def _records_like(src, items) -> Records:
    out = Records(items)
    out.excluded = list(getattr(src, "excluded", []))
    out.statements = list(getattr(src, "statements", []))
    return out


def efs_records(page, names, exclusions, directions) -> Records:
    """The records of one year page: per authority lad24cd, financial_year,
    amount_m (the year's figure), status, hra_only, cell_text and
    page_updated_at (plus name and table for the preview). names: a
    NameBook; exclusions: names skipped and counted; directions: the lad24cd
    with a capitalisation direction for the page's year. ValueError (naming
    the page, row and cell) on an unknown name, cell form or heading, a bare
    amount with no direction, or an authority on the page twice."""
    ident = page_identity(page)
    year = ident["year"]
    body = (_json(page).get("details") or {}).get("body")
    out = Records()
    out.excluded, out.statements = [], []
    seen = {}
    for heading, rows in tables(body):
        kind, detail = table_kind(heading)
        tag = heading or "the main table"
        if kind in ("main", "capitalisation-support") and detail and \
                detail != year:
            raise ValueError(f"the {year} page has a table under {heading!r}")
        if kind == "police":
            for name, cell in rows:
                if name not in exclusions:
                    raise ValueError(f"the {year} page's Police Force table "
                                     f"lists {name!r}, which is not an "
                                     "exclusion in s12_efs_names.json")
                out.excluded.append((name, kind, " | ".join(
                    _cell_lines(cell))))
            continue
        if kind == "other-years":
            code = match_name(detail, names.boundaries, names.aliases,
                              exclusions)
            for row_year, cell in rows:
                where = (f"the {year} page ({tag}), row {row_year!r}")
                label_ok(row_year)
                c = parse_cell(cell, where)
                figs = [s for s in c.statements if s.kind == "figure"]
                if row_year >= year or len(figs) != 1 or figs[0].year:
                    raise ValueError(f"{where}: expected one figure for an "
                                     f"earlier year, got {c.text!r}")
                if code:
                    out.statements.append({
                        "source_year": year, "name": detail, "lad24cd": code,
                        "year": row_year, "amount": figs[0].amount,
                        "text": c.text})
            continue
        for name, cell in rows:
            where = f"the {year} page ({tag}), row {name!r}"
            code = match_name(name, names.boundaries, names.aliases,
                              exclusions)
            if code is None:
                out.excluded.append((name, kind, " | ".join(
                    _cell_lines(cell))))
                continue
            c = parse_cell(cell, where)
            if c.withdrawn:
                amount, status = None, "withdrawn"
            else:
                f = c.figure(year)
                if f is None:
                    amount, status = None, "other-years-only"
                elif f.qual in QUAL_STATUS:
                    amount, status = f.amount, QUAL_STATUS[f.qual]
                elif code in directions:
                    amount, status = f.amount, "capitalisation-direction"
                else:
                    raise ValueError(
                        f"{where}: cell {c.text!r}: the {year} figure has no "
                        f"in-principle wording and the page lists no "
                        f"capitalisation direction for {name} covering "
                        f"{year}; not a status this loader knows")
            if code in seen:
                raise ValueError(f"{where}: {code} is already on the page "
                                 f"(row {seen[code]!r})")
            seen[code] = name
            out.append({"lad24cd": code, "financial_year": year,
                        "amount_m": amount, "status": status,
                        "hra_only": kind == "hra", "cell_text": c.text,
                        "page_updated_at": ident["public_updated_at"],
                        "name": name, "table": kind})
            for s in c.others(year):
                out.statements.append({
                    "source_year": year, "name": name, "lad24cd": code,
                    "year": s.year, "amount": s.amount, "text": s.text})
    return out


def other_year_statements(pages) -> list:
    """Each statement a page makes about another year, compared with that
    year's own page (pages: {year: Records}): outcome 'agrees' (the same
    amount), 'differs' (the year's page gives another amount, or none),
    'absent' (the year's page does not list the authority), 'no page' (that
    year is not read), or 'superseded on the same page' (a later statement on
    the same page about the same authority and year is the one compared).
    The year's own page always wins; this only lists."""
    own = {y: {r["lad24cd"]: r for r in recs} for y, recs in pages.items()}
    out = []
    for src in sorted(pages):
        sts = list(getattr(pages[src], "statements", []))
        last = {}
        for i, s in enumerate(sts):
            last[(s["lad24cd"], s["year"])] = i
        for i, s in enumerate(sts):
            r = dict(s, own_amount=None)
            y = s["year"]
            if last[(s["lad24cd"], y)] != i:
                r["outcome"] = "superseded on the same page"
            elif y not in own:
                r["outcome"] = "no page"
            elif s["lad24cd"] not in own[y]:
                r["outcome"] = "absent"
            else:
                r["own_amount"] = own[y][s["lad24cd"]]["amount_m"]
                r["outcome"] = ("agrees" if r["own_amount"] is not None
                                and r["own_amount"] == s["amount"]
                                else "differs")
            out.append(r)
    return out


def disagreements(statements) -> dict:
    """{year: [statement]} of the statements that differ from the year's own
    page."""
    out = {}
    for s in statements:
        if s["outcome"] == "differs":
            out.setdefault(s["year"], []).append(s)
    return dict(sorted(out.items()))


def apply_withdrawn_rule(by_period, held=None) -> tuple:
    """WITHDRAWN_ONLY_NOT_SUPPORT on the records read ({year: records}) and
    the held records of years not read (held): (kept {year: records without
    the named authorities' rows}, dropped [(lad24cd, year, cell_text)]).
    ValueError when a named authority has any row that is not withdrawn (the
    rule no longer holds) or an authority not named has only withdrawn rows
    (it needs a decision, then a name in the rule)."""
    rule = globals()["WITHDRAWN_ONLY_NOT_SUPPORT"]
    named = set(rule["codes"])
    allrecs = {}
    for y, recs in list((held or {}).items()) + list(by_period.items()):
        if y in by_period and recs is not by_period[y]:
            continue
        for r in recs:
            allrecs.setdefault(r["lad24cd"], []).append((y, r["status"]))
    bad = {c: v for c, v in allrecs.items() if c in named
           and any(s != "withdrawn" for _, s in v)}
    if bad:
        raise ValueError(f"{rule['name']} names "
                         + "; ".join(f"{c} (rows {sorted(v)})"
                                     for c, v in sorted(bad.items()))
                         + ", which now has a request that is not withdrawn; "
                         "the rule no longer holds for it: take it to Scott "
                         "and change the rule")
    loose = sorted(c for c, v in allrecs.items() if c not in named
                   and all(s == "withdrawn" for _, s in v))
    if loose:
        raise ValueError(f"{loose}: every EFS row is a withdrawn request "
                         f"({ {c: sorted(y for y, _ in allrecs[c]) for c in loose} }) "
                         f"but {rule['name']} does not name them; the EFS "
                         "flag for them is a decision for Scott: name them "
                         "in the rule, with the page text as evidence")
    kept, dropped = {}, []
    for y, recs in by_period.items():
        keep = []
        for r in recs:
            if r["lad24cd"] in named:
                dropped.append((r["lad24cd"], y, r.get("cell_text")))
            else:
                keep.append(r)
        kept[y] = _records_like(recs, keep)
    return kept, sorted(dropped)


def rows_content_sha(records) -> str:
    """sha256 of an EFS year's content: per row (sorted by lad24cd)
    lad24cd|amount_m at 3 dp|status|hra_only|cell_text, NULL as ''. Not
    page_updated_at (provenance)."""
    lines = []
    for r in sorted(records, key=lambda x: x["lad24cd"]):
        lines.append("|".join([r["lad24cd"], _amount_text(r.get("amount_m")),
                               r.get("status") or "",
                               "true" if r.get("hra_only") else "false",
                               r.get("cell_text") or ""]))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ranks, ledger and planning (EFS: one page is one year)
# ---------------------------------------------------------------------------

_ISO_Z = r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)"
_RANK_RE = re.compile(r"\bpage updated " + _ISO_Z)
_LEDGER_RE = re.compile(rf"(.*) \(EFS {LABEL}; page updated {_ISO_Z}\)")


def rank_text(rank) -> str:
    if rank is None:
        return "no page rank"
    return f"page updated {rank.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"


def release_rank(x) -> "datetime | None":
    """The rank an edition's or ledger row's source_file names ('page
    updated <iso>'), or a page identity's; None if it names none."""
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    return _when(mm.group(1)) if mm else None


def ledger_source(where, year, rank) -> str:
    return f"{where} (EFS {year}; {rank_text(rank)})"


def parse_ledger_source(s) -> "tuple | None":
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return mm.group(1), mm.group(2), _when(mm.group(3))


def tip_ranks(tips, checked, rank_of=None) -> dict:
    """{held period: the newest rank that stated it}: the tip edition's, or
    a later file the ledger records for the period (None: no rank, as
    loaded)."""
    rank_of = rank_of or release_rank
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [rank_of(s) for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, period) -> bool:
    """True when the ledger records this page's (where, sha256) for the
    period."""
    for s, h in checked.get(period, ()):
        parsed = parse_ledger_source(s)
        if h == sha and parsed and parsed[0] == where:
            return True
    return False


def plan_periods(pages, ranks, checked, *, recheck=None, allow_older=False,
                 only=None, stranded=()) -> tuple:
    """(new, revised, skipped {year: reason}) for the pages read ({year:
    {where, sha, rank}}): a year not held is new; a held year whose tip comes
    from a newer page is skipped as older unless allow_older (the older-page
    guard); a page the ledger records is skipped as unchanged unless --recheck
    names its year or the year is stranded; only (--year) limits the run to
    one year. ValueError when --recheck or --year names a year not read (or,
    for --recheck, not held)."""
    if recheck is not None and (recheck not in pages or recheck not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held year read in this "
                         f"run ({', '.join(sorted(pages))})")
    if only is not None and only not in pages:
        raise ValueError(f"--year {only}: no page read for it "
                         f"({', '.join(sorted(pages))})")
    new, revised, skipped = [], [], {}
    for y in sorted(pages):
        pg = pages[y]
        if only is not None and y != only:
            skipped[y] = f"not requested (--year {only})"
            continue
        if y not in ranks:
            new.append(y)
            continue
        t = ranks[y]
        if t is not None and t > pg["rank"] and not allow_older:
            skipped[y] = (f"older: its tip comes from a page of "
                          f"{rank_text(t)}, this page is "
                          f"{rank_text(pg['rank'])}")
            continue
        if y != recheck and y not in stranded and \
                ledger_complete(checked, pg["where"], pg["sha"], y):
            skipped[y] = ("unchanged: this page is already in the ledger "
                          "(--recheck YEAR reads it again)")
            continue
        revised.append(y)
    return new, revised, skipped


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

PARTIAL = "PARTIAL PAGE"


def _by_key(records, key=("lad24cd",)) -> dict:
    if len(key) == 1:
        return {r[key[0]]: r for r in records or ()}
    return {tuple(r[k] for k in key): r for r in records or ()}


def _same(a, b) -> bool:
    """Equal, NULL equal to NULL, NULL never equal to a value."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def efs_period_problems(new, tip, *, kind, rule_dropped=()) -> tuple:
    """(hard, soft, added) for one EFS year against the held edition (tip):
    hard problems no flag releases (a held authority missing from the page
    other than under the withdrawn-only rule; no rows), soft problems
    --acknowledge YEAR releases (any change of amount, status or hra_only),
    added the authorities new to the year (listed; refresh-latest needs
    --accept-key-changes)."""
    hard, soft = [], []
    if not new:
        hard.append(f"{PARTIAL}: no authority rows on the page")
    if kind == "new":
        return hard, soft, sorted(_by_key(new))
    nk, tk = _by_key(new), _by_key(tip)
    gone = sorted(set(tk) - set(nk) - set(rule_dropped))
    if gone:
        hard.append(f"{PARTIAL}: {len(gone)} authorit(ies) of the held "
                    f"edition not on the page: {', '.join(gone)} (a page "
                    "never removes a row; only the withdrawn-only rule does)")
    changed = []
    for k in sorted(set(nk) & set(tk)):
        diffs = [f"{c} {_show(tk[k][c])}->{_show(nk[k][c])}"
                 for c in EFS_VALUES if not _same(tk[k][c], nk[k][c])]
        if diffs:
            changed.append(f"{k} " + ", ".join(diffs))
    if changed:
        soft.append(f"{len(changed)} authorit(ies) change against the held "
                    "edition: " + "; ".join(changed))
    return hard, soft, sorted(set(nk) - set(tk))


def _show(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Decimal):
        return _amount_text(v)
    return str(v)


def efs_flips(new, tip) -> dict:
    """{lad24cd: (held amount text or None, new amount text or None)} of the
    amounts going to or from NULL against the held edition."""
    nk, tk = _by_key(new), _by_key(tip)
    out = {}
    for k in sorted(set(nk) & set(tk)):
        a, b = tk[k]["amount_m"], nk[k]["amount_m"]
        if (a is None) != (b is None):
            out[k] = (None if a is None else _amount_text(a),
                      None if b is None else _amount_text(b))
    return out


def flip_problems(name, period, flips) -> list:
    """Problems (empty = released) of the flips of a year under
    ACKNOWLEDGED_FLIPS[name]: exactly the listed cells, nothing else."""
    if not flips:
        return []
    msg = (f"{len(flips)} amount(s) go to or from NULL against the held "
           f"edition (rule 1.10): " + ", ".join(
               f"{k} {a or 'NULL'}->{b or 'NULL'}"
               for k, (a, b) in sorted(flips.items())))
    if not name:
        return [msg + "; released only by a named entry: --acknowledge-flips "
                "NAME (ACKNOWLEDGED_FLIPS in the loader)"]
    want = globals()["ACKNOWLEDGED_FLIPS"][name]["periods"].get(period, {})
    if dict(want) != flips:
        return [msg + f"; --acknowledge-flips {name} lists "
                f"{dict(want) or 'nothing'} for {period}, not exactly these"]
    return []


def _differs(new, old, cols, key=("lad24cd",)) -> bool:
    nk, ok = _by_key(new, key), _by_key(old, key)
    if set(nk) != set(ok):
        return True
    return any(not _same(nk[k].get(c), ok[k].get(c)) for k in nk for c in cols)


# ---------------------------------------------------------------------------
# The S.114 register (manual input)
# ---------------------------------------------------------------------------

@dataclass
class S114Register:
    path: Path
    as_at: date
    rows: list
    unevidenced: list
    by_period: dict
    warnings: list


def _blank(v) -> "str | None":
    v = (v or "").strip()
    return v or None


def _iso(v, what, where) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v or ""):
        halt(f"{where}: {what} {v!r} is not a yyyy-mm-dd date")
    try:
        return date.fromisoformat(v)
    except ValueError:
        halt(f"{where}: {what} {v!r} is not a date")


def read_s114(path, boundaries=None) -> S114Register:
    """The curated S.114 register through manual_input: the file must sit
    under S114_ROOTS (data/raw, data/reference), start 'register_as_at,
    <date>' and carry exactly S114_HEADER. Each row: lad24cd in la_boundaries
    (boundaries {lad24nm: lad24cd}) with la_name its lad24nm, unless
    attribution = 'predecessor' with successor codes (';'-joined, each in
    la_boundaries) and a note (the gate 14 rule); notice_date a date;
    date_confirmed 'exact' or 'approximate - month only confirmed'; evidence
    a URL that is not Wikipedia, and checked_on a date, where given. A row
    without all three evidence columns is in .unevidenced (still a row). A
    financial_year that is not the date's April-March year is a warning.
    Halts on everything else."""
    p = manual_input.require_under(path, globals()["S114_ROOTS"])
    as_at, rows = manual_input.read_register(p, S114_HEADER, S114_EVIDENCE,
                                             S114_KEY)
    codes = set(boundaries.values()) if boundaries is not None else None
    name_of = ({c: n for n, c in boundaries.items()}
               if boundaries is not None else {})
    by_period, warnings = {}, []
    for r in rows:
        where = f"{p.name} row {r['la_name']!r} {r['lad24cd']} {r['notice_date']}"
        code = r["lad24cd"]
        if not CODE_RE.fullmatch(code):
            halt(f"{where}: lad24cd {code!r} is not a GSS code")
        d = _iso(r["notice_date"], "notice_date", where)
        try:
            fy = label_ok(r["financial_year"])
        except ValueError as e:
            halt(f"{where}: {e}")
        if r["date_confirmed"] not in DATE_CONFIRMED:
            halt(f"{where}: date_confirmed {r['date_confirmed']!r} is not one "
                 f"of {list(DATE_CONFIRMED)}")
        attr = r["attribution"]
        if attr not in ATTRIBUTIONS:
            halt(f"{where}: attribution {attr!r} is not one of "
                 f"{list(ATTRIBUTIONS)}")
        succ = [s.strip() for s in r["successor_codes"].split(";")
                if s.strip()] or None
        note = _blank(r["attribution_note"])
        if attr == "predecessor":
            if not succ or not note:
                halt(f"{where}: attribution 'predecessor' needs successor "
                     "codes and an attribution_note (the gate 14 rule)")
            for s in succ:
                if not CODE_RE.fullmatch(s) or (codes is not None
                                                and s not in codes):
                    halt(f"{where}: successor code {s!r} is not in "
                         "la_boundaries")
        else:
            if succ or note:
                halt(f"{where}: a 'direct' row carries successor codes or an "
                     "attribution_note")
            if codes is not None and code not in codes:
                halt(f"{where}: {code} is not in la_boundaries; an issuer that "
                     "no longer exists needs attribution 'predecessor' with "
                     "successor codes and a note")
            if codes is not None and name_of.get(code) != r["la_name"]:
                halt(f"{where}: la_name {r['la_name']!r} is not the lad24nm of "
                     f"{code} ({name_of.get(code)!r})")
        url = _blank(r["evidence_url"])
        if url:
            host = urlparse(url).netloc
            if urlparse(url).scheme not in ("http", "https") or not host:
                halt(f"{where}: evidence_url {url!r} is not a web address")
            if host == "wikipedia.org" or host.endswith(".wikipedia.org"):
                halt(f"{where}: evidence_url is Wikipedia, which is not "
                     "evidence (the council's own report, minutes or "
                     "statement is)")
        checked = _blank(r["checked_on"])
        checked = _iso(checked, "checked_on", where) if checked else None
        if fy_of(d) != fy:
            warnings.append(f"{where}: financial_year {fy} is not the "
                            f"April-March year of {d} ({fy_of(d)})")
        by_period.setdefault(fy, []).append({
            "lad24cd": code, "notice_date": d, "financial_year": fy,
            "reason": _blank(r["reason"]),
            "date_confirmed": r["date_confirmed"], "attribution": attr,
            "successor_codes": succ, "attribution_note": note,
            "evidence_url": url, "evidence_title": _blank(r["evidence_title"]),
            "checked_on": checked})
    return S114Register(p, as_at, list(rows), list(rows.unevidenced),
                        dict(sorted(by_period.items())), warnings)


def s114_content_sha(records) -> str:
    """sha256 of an S.114 year's content: per row (sorted by key) every
    compared column, NULL as '', successor codes ';'-joined, dates ISO."""
    def cell(v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return ";".join(v)
        if isinstance(v, date):
            return v.isoformat()
        return str(v)
    lines = ["|".join([r["lad24cd"], cell(r["notice_date"])]
                      + [cell(r.get(c)) for c in S114_COMPARED])
             for r in sorted(records, key=lambda x: (x["lad24cd"],
                                                     str(x["notice_date"])))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


_S114_RANK_RE = re.compile(r"\bregister_as_at ([0-9]{4}-[0-9]{2}-[0-9]{2})\b")
_S114_LEDGER_RE = re.compile(r"(.*) \(S\.114 register; (?:register_as_at "
                             r"[0-9]{4}-[0-9]{2}-[0-9]{2}|legacy format, no "
                             r"register_as_at)\)")


def s114_rank(x) -> "date | None":
    mm = _S114_RANK_RE.search(str(x or ""))
    return date.fromisoformat(mm.group(1)) if mm else None


def s114_ledger_source(where, as_at) -> str:
    return (f"{where} (S.114 register; "
            + (f"register_as_at {as_at.isoformat()}" if as_at
               else "legacy format, no register_as_at") + ")")


def s114_period_problems(new, tip, *, kind) -> tuple:
    """(hard, soft, listed) of one S.114 year against the held edition: hard
    a held notice missing from the register (never removed by the loader,
    unless named in S114_REMOVALS) or no notices left; soft a notice added,
    a new year, or a held value changed (--acknowledge YEAR); listed the
    date corrections, named removals and evidence changes."""
    hard, soft, listed = [], [], []
    removals = globals()["S114_REMOVALS"]
    if kind == "new":
        soft.append(f"a new year of notices ({len(new)}): s114_flag may "
                    "change; read the preview")
        return hard, soft, listed
    nk, tk = _by_key(new, S114_KEY), _by_key(tip, S114_KEY)
    removed = sorted(set(tk) - set(nk))
    added = sorted(set(nk) - set(tk))
    for r in list(removed):
        old = tk[r]
        pair = [a for a in added if a[0] == r[0]
                and old["date_confirmed"] == DATE_CONFIRMED[1]
                and nk[a]["date_confirmed"] == "exact"
                and (a[1].year, a[1].month) == (r[1].year, r[1].month)
                and all(_same(old[c], nk[a][c]) for c in
                        ("reason", "attribution", "successor_codes",
                         "attribution_note"))]
        if len(pair) == 1:
            a = pair[0]
            listed.append(f"date correction {r[0]} {r[1]} (month only) -> "
                          f"{a[1]} (exact); refresh-latest needs "
                          "--accept-key-changes")
            removed.remove(r)
            added.remove(a)
    for r in list(removed):
        named = removals.get((r[0], r[1].isoformat()))
        if named:
            listed.append(f"named removal {r[0]} {r[1]} (S114_REMOVALS: "
                          f"{named.get('decided')}; searched: "
                          f"{named.get('searched')})")
            removed.remove(r)
    if removed:
        hard.append(f"{len(removed)} held notice(s) not in the register: "
                    + ", ".join(f"{c} {d}" for c, d in removed)
                    + "; the loader never removes a notice (s114_flag); a "
                    "removal is Scott's decision, named in S114_REMOVALS")
    if not new:
        hard.append("no notices left for the year; an empty year cannot be "
                    "stored as an edition")
    if added:
        soft.append(f"{len(added)} notice(s) added: "
                    + ", ".join(f"{c} {d}" for c, d in added)
                    + " (s114_flag may change)")
    changed = []
    for k in sorted(set(nk) & set(tk)):
        diffs = [c for c in S114_VALUES if not _same(tk[k][c], nk[k][c])]
        if diffs:
            changed.append(f"{k[0]} {k[1]} {', '.join(diffs)}")
        ev = [c for c, _ in S114_EXTRA_TYPES if not _same(tk[k][c], nk[k][c])]
        if ev:
            listed.append(f"evidence {k[0]} {k[1]}: {', '.join(ev)} "
                          + ("filled" if all(tk[k][c] is None for c in ev)
                             else "changed"))
    if changed:
        soft.append(f"{len(changed)} held notice(s) change: "
                    + "; ".join(changed))
    return hard, soft, listed


# ---------------------------------------------------------------------------
# Specs and profiles
# ---------------------------------------------------------------------------

def tip_row_count(editions_table: str):
    """expected_rows_per_period for status: the tip edition's row count (a
    year's authorities, or notices, are its own)."""
    core._ident(editions_table)

    def count(cur, period):
        cur.execute(f"SELECT DISTINCT edition, supersedes FROM "
                    f"public.{editions_table} WHERE financial_year = %s",
                    (period,))
        try:
            tip = core._chain_tip(cur.fetchall(), str(period))
        except (LookupError, ValueError):
            return None
        cur.execute(f"SELECT COUNT(*) FROM public.{editions_table} WHERE "
                    "financial_year = %s AND edition = %s", (period, tip))
        return cur.fetchone()[0]
    return count


_STATUS_LIST = ", ".join(f"'{s}'" for s in STATUSES)
_NULL_LIST = ", ".join(f"'{s}'" for s in NULL_STATUSES)

SPEC_EFS = core.EditionSpec(
    name="s12_efs",
    live_table="la_efs_support",
    editions_table="la_efs_support_editions",
    key_cols=("lad24cd",),
    period_col="financial_year",
    value_cols=EFS_VALUE_TYPES,
    extra_cols=EFS_EXTRA_TYPES,
    refresh_cols=EFS_VALUES + ("source", "loaded_at"),
    refresh_from=(("source", "release_label"), ("loaded_at", "loaded_at")),
    key_types=(("lad24cd", "varchar(9) NOT NULL"),
               ("financial_year", "varchar(7) NOT NULL")),
    fk_la_boundaries=True,
    table_constraints=(
        f"CONSTRAINT la_efs_support_editions_status_chk CHECK (status IN "
        f"({_STATUS_LIST}))",
        f"CONSTRAINT la_efs_support_editions_amount_chk CHECK ((amount_m IS "
        f"NULL) = (status IN ({_NULL_LIST})))"),
    refresh_key_changes=True,
    expected_rows_per_period=tip_row_count("la_efs_support_editions"),
)

SPEC_S114 = core.EditionSpec(
    name="s12_s114",
    live_table="la_s114_notices",
    editions_table="la_s114_notices_editions",
    key_cols=S114_KEY,
    period_col="financial_year",
    value_cols=S114_VALUE_TYPES,
    extra_cols=S114_EXTRA_TYPES,
    refresh_cols=S114_VALUES + ("source", "loaded_at"),
    refresh_from=(("source", "release_label"), ("loaded_at", "loaded_at")),
    key_types=(("lad24cd", "varchar(9) NOT NULL"),
               ("notice_date", "date NOT NULL"),
               ("financial_year", "varchar(7) NOT NULL")),
    fk_la_boundaries=False,      # an abolished issuer is kept (gate 14)
    table_constraints=(
        "CONSTRAINT la_s114_notices_editions_attribution_chk CHECK "
        "(((attribution = ANY (ARRAY['direct'::text, 'predecessor'::text])) "
        "AND ((attribution = 'direct'::text) OR ((successor_codes IS NOT "
        "NULL) AND (attribution_note IS NOT NULL)))))",
        "CONSTRAINT la_s114_notices_editions_date_confirmed_chk CHECK "
        "(date_confirmed IN ('exact', 'approximate - month only "
        "confirmed'))"),
    refresh_key_changes=True,
    expected_rows_per_period=tip_row_count("la_s114_notices_editions"),
)

SPECS = {"s12_efs": SPEC_EFS, "s12_s114": SPEC_S114}
PARTS = {"efs": "s12_efs", "s114": "s12_s114"}


def _no_label(fetched_on) -> str:
    raise ValueError("an S12 release label comes from the page or register; "
                     "store through apply_part(...)")


def check_efs_records(records, period) -> None:
    """ValueError unless the year's EFS records are whole: each of this year,
    one per lad24cd, a known status, amount NULL exactly for the NULL
    statuses, hra_only a boolean."""
    seen = set()
    for r in records:
        if r.get("financial_year") != period:
            raise ValueError(f"{period}: a record of {r.get('financial_year')!r}")
        if r["lad24cd"] in seen:
            raise ValueError(f"{period}: {r['lad24cd']} appears twice")
        seen.add(r["lad24cd"])
        if r["status"] not in STATUSES:
            raise ValueError(f"{period}: {r['lad24cd']} status {r['status']!r}")
        if (r["amount_m"] is None) != (r["status"] in NULL_STATUSES):
            raise ValueError(f"{period}: {r['lad24cd']} amount "
                             f"{r['amount_m']} with status {r['status']}")
        if not isinstance(r["hra_only"], bool):
            raise ValueError(f"{period}: {r['lad24cd']} hra_only not a boolean")


def check_s114_records(records, period) -> None:
    seen = set()
    for r in records:
        if r.get("financial_year") != period:
            raise ValueError(f"{period}: a record of {r.get('financial_year')!r}")
        k = (r["lad24cd"], r["notice_date"])
        if k in seen:
            raise ValueError(f"{period}: {k} appears twice")
        seen.add(k)
        if r["date_confirmed"] not in DATE_CONFIRMED or \
                r["attribution"] not in ATTRIBUTIONS:
            raise ValueError(f"{period}: {k} date_confirmed or attribution")


PROFILES = {
    "s12_efs": pe.Profile(
        spec=SPEC_EFS, value_cols=EFS_COMPARED, run_agent=RUN_AGENT,
        run_source=RUN_SOURCE,
        heading="S12 MHCLG Exceptional Financial Support (la_efs_support)",
        default_source_file="MHCLG Exceptional Financial Support year page",
        expected_areas=None, release_label=_no_label,
        content_sha256=rows_content_sha, check_records=check_efs_records,
        file_checks=True, savepoint="s12_efs_period",
        example_label="(amount_m, status, hra_only, cell_text)"),
    "s12_s114": pe.Profile(
        spec=SPEC_S114, value_cols=S114_COMPARED, run_agent=RUN_AGENT,
        run_source=RUN_SOURCE,
        heading="S12 section 114 notices (la_s114_notices)",
        default_source_file="curated S.114 register",
        expected_areas=None, release_label=_no_label,
        content_sha256=s114_content_sha, check_records=check_s114_records,
        file_checks=True, savepoint="s12_s114_period",
        example_label="(reason, date_confirmed, attribution, ..., evidence)"),
}


def profile(name) -> pe.Profile:
    """PROFILES[name] bound to the current SPECS[name] (looked up when
    called, so tests can swap the specs)."""
    return globals()["PROFILES"][name].with_spec(globals()["SPECS"][name])


def _is_s114(spec) -> bool:
    return tuple(spec.key_cols) == S114_KEY


def refresh_spec(spec):
    """The spec refresh-latest uses: the editions-only columns (cell_text,
    page_updated_at; the evidence columns) are not live columns, so the
    key-change insert must not name them; everything compared stays the
    same (they are not refresh columns)."""
    return dataclasses.replace(spec, extra_cols=())


def _late(name: str):
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: both editions tables, their append-only triggers and the two
    file-check ledgers. Idempotent; no value is written; the live tables
    are not touched."""
    for name in SPECS:
        prof = profile(name)
        core.create_schema(cur, prof.spec)
        pe.create_file_checks(cur, prof)


def insert_live(cur, profile, period: str, records: list) -> None:
    """A new year's live rows: key, period, the live value columns and
    source (each record's live_source); the editions-only columns are not
    written; loaded_at takes its default."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    values = S114_VALUES if _is_s114(spec) else EFS_VALUES
    cols = tuple(spec.key_cols) + (spec.period_col,) + values + ("source",)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [tuple(r[k] for k in spec.key_cols) + (period,)
         + tuple(r[c] for c in values) + (r["live_source"],)
         for r in records], page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a year's edition records: source_file, the release label, the
    published date and the files read ((ledger source, sha256),)."""
    source_file: str
    label: str
    published: date
    files: tuple


def _tip_label(cur, spec, period) -> "str | None":
    try:
        tip = core.chain_tip(cur, spec, period)
    except (LookupError, ValueError):
        return None
    cur.execute(f"SELECT DISTINCT release_label FROM public.{spec.editions_table}"
                f" WHERE {spec.period_col} = %s AND edition = %s", (period, tip))
    r = cur.fetchone()
    return r[0] if r else None


def compare_profile(cur, prof, period):
    """The profile a year is compared with: an EFS tip that is edition 1 'as
    loaded' holds no cell_text, so it is compared on the live values only
    (a page reproducing it is unchanged)."""
    if _is_s114(prof.spec):
        return prof
    label = _tip_label(cur, prof.spec, period) or ""
    if label.startswith("as loaded"):
        return dataclasses.replace(prof, value_cols=EFS_VALUES)
    return prof


def apply_part(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for one year, then its ledger rows, inside the
    caller's per-year savepoint: a new year's edition 1, live rows and
    ledger row commit or roll back together. Each record's live_source is the
    edition's label; a stranded year gets the tip edition's."""
    cmp_prof = compare_profile(cur, profile, period)
    kind = pe.classify_period(cur, cmp_prof, period, records)
    label = info.label
    if kind == LIVE_MISSING:
        label = _tip_label(cur, profile.spec, period) or info.label
    records = [dict(r, live_source=label) for r in records]
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


def fetch_page(base_path, session=None, dest=None) -> tuple:
    """(page JSON, final URL) of a year page from the content API
    (redirects followed). The reply is saved to data/raw/s12_efs/
    efs_<yyyy-yy>_content_<today>.json, the year from the page's own title;
    a same-named file with the same content is kept, one with different
    content is never replaced (the reply is saved beside it as
    <stem>-<sha8>.json). A reply that is not a year page halts, nothing
    saved."""
    dest = Path(dest or globals()["RAW_DIR"])
    url = API + base_path
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    try:
        pg = json.loads(body)
        year = page_identity(pg)["year"]
    except (ValueError, TypeError) as e:
        halt(f"{final}: not an EFS year page ({e}); nothing saved")
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"efs_{year}_content_{date.today():%Y-%m-%d}.json"
    new_sha = hashlib.sha256(body).hexdigest()
    if target.exists():
        old = content_sha256(target)
        if old == new_sha:
            print(f"{target.name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
        else:
            alt = dest / f"{target.stem}-{new_sha[:8]}{target.suffix}"
            if not (alt.exists() and content_sha256(alt) == new_sha):
                alt.write_bytes(body)
            print(f"NOTE: {target.name} already exists with different content "
                  f"(sha256 {old[:16]}, new {new_sha[:16]}); kept it untouched "
                  f"and saved this reply as {alt.name}")
    else:
        target.write_bytes(body)
        print(f"saved {target.name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    return pg, final


def _file_source(path) -> str:
    """Where a file was read: the path under the repo (data/...), else the
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
    """The year's rows as records: the given edition's (every data column and
    release_label as live_source), or the live table's (edition None: the
    live columns, source as live_source, the editions-only columns None)."""
    s114 = _is_s114(spec)
    values = S114_VALUES if s114 else EFS_VALUES
    extras = tuple(c for c, _ in (S114_EXTRA_TYPES if s114
                                  else EFS_EXTRA_TYPES))
    keys = tuple(spec.key_cols)
    if edition is None:
        names = keys + values + ("source",)
        cur.execute(f"SELECT {', '.join(names)} FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (period,))
        out = []
        for row in cur.fetchall():
            r = dict(zip(names, row))
            r["live_source"] = r.pop("source")
            for c in extras:
                r[c] = None
            out.append(r)
    else:
        names = keys + values + extras + ("release_label",)
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (period, edition))
        out = []
        for row in cur.fetchall():
            r = dict(zip(names, row))
            r["live_source"] = r.pop("release_label")
            out.append(r)
    for r in out:
        r[spec.period_col] = str(period)
    return out


def tip_info(cur, spec) -> dict:
    """{period: {edition, source_file, label, rank}} of every period with
    editions (rank from the tip's source_file: 'page updated' for EFS,
    'register_as_at' for S.114; None for 'as loaded')."""
    rank_of = s114_rank if _is_s114(spec) else release_rank
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.editions_table}")
    out = {}
    for (p,) in cur.fetchall():
        tip = core.latest_edition(cur, spec, str(p))
        cur.execute(f"SELECT DISTINCT source_file, release_label FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (p, tip))
        rows = cur.fetchall()
        sf, lb = rows[0] if len(rows) == 1 else (None, None)
        out[pe._p(p)] = {"edition": tip, "source_file": sf, "label": lb,
                         "rank": rank_of(sf)}
    return out


def legacy_tips(cur, spec) -> dict:
    """Before the editions tables exist: {held live period: {rank None}}."""
    cur.execute(f"SELECT DISTINCT {spec.period_col} FROM "
                f"public.{spec.live_table}")
    return {pe._p(p): {"edition": None, "source_file": None, "label": None,
                       "rank": None} for (p,) in cur.fetchall()}


def ledger_checks(cur, prof) -> dict:
    """{period: {(source_file, file_sha256)}} of the ledger."""
    pc = prof.spec.period_col
    cur.execute(f"SELECT DISTINCT {pc}, source_file, file_sha256 "
                f"FROM public.{pe.file_checks_table(prof)}")
    out = {}
    for p, f, s in cur.fetchall():
        out.setdefault(pe._p(p), set()).add((f, s))
    return out


def _boundaries(cur) -> dict:
    cur.execute("SELECT lad24nm, lad24cd FROM public.la_boundaries")
    return dict(cur.fetchall())


def check_aliases(cur, aliases, boundaries) -> None:
    """Halts unless every alias names a lad24cd in la_boundaries, no alias is
    itself a lad24nm, and an alias naming an abolished authority has its
    la_code_lookup row (predecessor -> lad24cd)."""
    codes = set(boundaries.values())
    bad = [f"{n} -> {c}" for n, c in aliases.items() if c not in codes]
    bad += [f"{n} is itself a lad24nm" for n in aliases if n in boundaries]
    for n, (old, new) in alias_predecessors().items():
        cur.execute("SELECT 1 FROM public.la_code_lookup WHERE old_code = %s "
                    "AND new_code = %s", (old, new))
        if cur.fetchone() is None:
            bad.append(f"{n}: no la_code_lookup row {old} -> {new}")
    if bad:
        halt("s12_efs_names.json does not hold: " + "; ".join(bad))


# ---------------------------------------------------------------------------
# ddl, status, run log, refresh-latest
# ---------------------------------------------------------------------------

def log_run(cur, rows_written: int, notes: str, started_at=None) -> None:
    """The pipeline_run_log row for a committed run (agent RUN_AGENT, source
    '12'). Called only on committed load, migrate-legacy and restore-edition
    runs, partial runs included."""
    pe.log_run(cur, profile("s12_efs"), rows_written, notes, started_at)


def cmd_ddl(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for name in SPECS:
                prof = profile(name)
                for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
                    print(f"{t}: {'exists' if table_exists(cur, t) else 'does not exist'}")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur)
        pe.finish(conn, args, "ddl: la_efs_support_editions, "
                              "la_s114_notices_editions, their triggers and "
                              "ledgers present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _tables_ready(cur, name) -> list:
    prof = profile(name)
    return [t for t in (prof.spec.editions_table, pe.file_checks_table(prof))
            if not table_exists(cur, t)]


def unevidenced_count(cur, spec) -> int:
    """Notices in the latest editions without all three evidence columns."""
    n = 0
    for p, t in tip_info(cur, spec).items():
        n += sum(1 for r in records(cur, spec, p, t["edition"])
                 if any(r[c] is None for c, _ in S114_EXTRA_TYPES))
    return n


def status_lines(cur, name) -> list:
    prof = profile(name)
    s = prof.spec
    lt = pe.file_checks_table(prof)
    lines = []
    for p, t in sorted(tip_info(cur, s).items()):
        rank = t["rank"]
        rt = (rank_text(rank) if not _is_s114(s) else
              (f"register_as_at {rank}" if rank else "no register_as_at"))
        line = f"  {p}: tip edition {t['edition']} ({rt})"
        cur.execute(f"SELECT source_file, outcome FROM public.{lt} WHERE "
                    f"{s.period_col} = %s ORDER BY id DESC LIMIT 1", (p,))
        last = cur.fetchone()
        line += (f"; ledger latest: {last[1]} {last[0]}" if last
                 else "; no ledger row")
        lines.append(line)
    if _is_s114(s):
        lines.append(f"  S.114: {unevidenced_count(cur, s)} notices without "
                     "evidence in the latest editions")
    return lines


def status_ok(cur, name) -> bool:
    return pe.status(cur, profile(name))["ok"]


def cmd_status(args) -> int:
    """status: what needs action for both tables; exit 1 if anything, or
    (with a clean message, nothing created) if a table does not exist."""
    conn = _conn(False)
    rc = 0
    try:
        with conn.cursor() as cur:
            for name in SPECS:
                prof = profile(name)
                missing = _tables_ready(cur, name)
                if missing:
                    print(f"{prof.heading}: {' and '.join(missing)} not "
                          "present yet; run `ddl --commit`, then "
                          "`migrate-legacy --commit` (S12 has no sync-new)")
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
    edition on the key, the period and the live value columns, NULL-safe,
    both ways."""
    values = S114_VALUES if _is_s114(spec) else EFS_VALUES
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


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift P] [--accept-key-changes P] [--commit
    | --simulate]: for both tables, the engine's refresh (each year's latest
    edition copied into live: the live values, source from the edition's
    release label and loaded_at, under the before/after hash guard), keys
    added and removed only for a year named in --accept-key-changes, then
    the after-check live_equals_tip. A named year must have drift (or key
    changes) in at least one table."""
    writing = args.commit or args.simulate
    drift_args = tuple(args.accept_drift or ())
    kc_args = tuple(args.accept_key_changes or ())
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans = {}
            for name in SPECS:
                missing = _tables_ready(cur, name)
                if missing:
                    halt(f"{', '.join(missing)} not present yet; run `ddl "
                         "--commit`, then `migrate-legacy --commit`")
                spec = profile(name).spec
                tips, new, errors = core.latest_map(cur, spec)
                if new:
                    halt(f"{spec.live_table}: years with no editions "
                         f"{[pe._p(x) for x in new]}; run migrate-legacy first")
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {m}" for p, m in errors.items()))
                drifted = {pe._p(p): p for p, tip in tips.items()
                           if core.classify_period(cur, spec, p, tip)[0]
                           == "drift"}
                accept = tuple(drifted[a] for a in drift_args if a in drifted)
                plan, _ = core._plan(cur, spec, accept)
                keyed = {pe._p(p): p for p in core.key_change_periods(plan)}
                accept_kc = tuple(keyed[a] for a in kc_args if a in keyed)
                plans[name] = (spec, accept, accept_kc, plan, set(drifted),
                               set(keyed))
            stray = sorted(set(drift_args) - set().union(
                *(v[4] for v in plans.values())))
            if stray:
                halt(f"--accept-drift {stray}: not drifted in either table, "
                     "nothing to accept")
            stray = sorted(set(kc_args) - set().union(
                *(v[5] for v in plans.values())))
            if stray:
                halt(f"--accept-key-changes {stray}: no keys added or removed "
                     "in those years in either table, nothing to accept")
            for name, (spec, accept, accept_kc, plan, _, _) in plans.items():
                counts = core.refresh_counts(cur, spec, accept)
                print(f"{spec.live_table}: rows refresh-latest would write: "
                      + (", ".join(f"{pe._p(p)}={n}" for p, n in counts.items())
                         or "none") + f" (total {sum(counts.values())})")
                lines = core.key_change_lines(plan, accept_kc, name=pe._p)
                print(f"{spec.live_table}: keys added and removed:"
                      + ("" if lines else " none"))
                for line in lines:
                    print(f"  {line}")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            msgs = []
            for name, (spec, accept, accept_kc, _, _, _) in plans.items():
                pe._refresh_guard(cur, spec, accept, accept_kc)
                res = core.refresh_latest(cur, refresh_spec(spec), accept,
                                          accept_kc)
                if res["updated"]:
                    bad = live_equals_tip(cur, spec, sorted(res["updated"]))
                    if bad:
                        halt(f"refresh-latest: {spec.live_table} differs from "
                             "the latest edition after the refresh, rolled "
                             "back: " + "; ".join(bad[:6]))
                keys = ""
                if res["inserted"] or res["deleted"]:
                    keys = (f"; key changes {sum(res['inserted'].values())} "
                            f"inserted, {sum(res['deleted'].values())} deleted "
                            f"in {sorted(pe._p(x) for x in res['inserted'])}")
                msgs.append(f"{spec.live_table}: {res['rows']} live rows "
                            f"refreshed in "
                            f"{sorted(pe._p(x) for x in res['updated'])}{keys}")
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
    """One part's prepared run (EFS or S.114), before the acknowledgements
    are applied."""
    part: str
    prof: object
    has_ed: bool
    by_period: dict = dataclasses.field(default_factory=dict)
    new: list = dataclasses.field(default_factory=list)
    revised: list = dataclasses.field(default_factory=list)
    skipped: dict = dataclasses.field(default_factory=dict)
    hard: dict = dataclasses.field(default_factory=dict)
    soft: dict = dataclasses.field(default_factory=dict)
    flips: dict = dataclasses.field(default_factory=dict)
    reissue: dict = dataclasses.field(default_factory=dict)
    older: dict = dataclasses.field(default_factory=dict)
    info_args: dict = dataclasses.field(default_factory=dict)
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


def _year_arg(text) -> str:
    try:
        return label_ok(str(text or ""))
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _collection():
    coll = fetch_json(COLLECTION_PATH)
    try:
        year_pages(coll)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    return coll


def _print_page(ident, where):
    print(f"page: {ident['title']} ({where}); public_updated_at "
          f"{ident['public_updated_at']:%Y-%m-%dT%H:%M:%SZ}; rank "
          f"{rank_text(ident['rank'])}")
    for ts, note in ident["change_history"][:3]:
        print(f"  change_history {ts:%Y-%m-%d}: {note[:160]}")


def _efs_pages(args, held) -> dict:
    """{year: {page, ident, where, sha, rank, how, file_name}} of the pages
    this run reads: --page-file files (under PAGE_ROOTS; with --no-page
    nothing is fetched, otherwise the collection is read to check each file's
    year is listed with its base path), or every year page the collection
    lists, fetched (and saved) through the content API."""
    out = {}
    if args.page_file:
        coll = None
        if not args.no_page:
            coll = _collection()
            try:
                yp = year_pages(coll, held)
            except ValueError as e:
                halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
        for f in args.page_file:
            p = manual_input.require_under(f, globals()["PAGE_ROOTS"])
            ident_file = manual_input.file_identity(p)
            try:
                pg = json.loads(p.read_text(encoding="utf-8"))
                ident = page_identity(pg)
            except (ValueError, TypeError) as e:
                halt(f"--page-file {p}: not an EFS year page ({e}); nothing "
                     "stored")
            y = ident["year"]
            if y in out:
                halt(f"--page-file: two files for {y}")
            how = "saved page file" + (", --no-page: the collection was not "
                                       "read" if args.no_page else "")
            print(f"page file given: {_file_source(p)} (file sha256 "
                  f"{ident_file['sha256'][:16]}); its identity is the page's "
                  "own title, table header and dates")
            if coll is not None:
                if yp.get(y) != ident["base_path"]:
                    halt(f"--page-file {p}: the collection lists "
                         f"{yp.get(y)!r} for {y}, the file is "
                         f"{ident['base_path']!r}")
                listed = yp.updated.get(y)
                if listed and _when(listed) > ident["rank"]:
                    print(f"NOTE: the collection lists {y} as updated "
                          f"{listed}, later than this file "
                          f"({rank_text(ident['rank'])}): the live page may "
                          "be newer than the file")
            out[y] = {"page": pg, "ident": ident, "where": _file_source(p),
                      "sha": page_sha(pg), "rank": ident["rank"], "how": how,
                      "file_name": p.name}
        return out
    coll = fetch_json(COLLECTION_PATH)
    try:
        yp = year_pages(coll, held)
    except ValueError as e:
        halt(f"{GOV_UK}{COLLECTION_PATH}: {e}")
    print(f"collection: {COLLECTION_TITLE} ({GOV_UK}{COLLECTION_PATH}): "
          f"year pages {', '.join(sorted(yp))}")
    for t in yp.others:
        print(f"  listed, not a year page: {t!r}")
    for y in sorted(yp):
        pg, final = fetch_page(yp[y])
        try:
            ident = page_identity(pg)
        except ValueError as e:
            halt(f"{final}: {e}; nothing stored")
        if ident["year"] != y:
            halt(f"{final}: the page is for {ident['year']}, the collection "
                 f"lists it for {y}")
        out[y] = {"page": pg, "ident": ident, "where": final,
                  "sha": page_sha(pg), "rank": ident["rank"],
                  "how": "content API", "file_name": Path(yp[y]).name}
    return out


def _tip_records(cur, spec, p, tips, has_ed) -> list:
    if has_ed:
        return records(cur, spec, p, tips[p]["edition"])
    return records(cur, spec, p)


def _held_records(cur, spec, tips, has_ed) -> dict:
    return {p: _tip_records(cur, spec, p, tips, has_ed) for p in tips}


def _flag_set(by_period) -> set:
    return {r["lad24cd"] for recs in by_period.values() for r in recs}


def _prepare_tables(cur, name, writing):
    prof = profile(name)
    spec = prof.spec
    missing = _tables_ready(cur, name)
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
            halt(f"{spec.live_table}: live years with no editions "
                 f"{[pe._p(x) for x in new_live]}; run migrate-legacy first")
        tips = tip_info(cur, spec)
        checked = ledger_checks(cur, prof)
        stranded = pe.live_missing_periods(cur, prof)
    else:
        print(f"NOTE: {', '.join(missing)} not present: this preview compares "
              f"with the LIVE table {spec.live_table}")
        tips, checked, stranded = legacy_tips(cur, spec), {}, []
    return prof, spec, has_ed, tips, checked, stranded


def _prepare_efs(args, cur, writing) -> Plan:
    prof, spec, has_ed, tips, checked, stranded = _prepare_tables(
        cur, "s12_efs", writing)
    plan = Plan("efs", prof, has_ed)
    ranks = tip_ranks(tips, checked)
    held = sorted(ranks)
    print(f"EFS held years: {', '.join(held) or 'none'}")
    pages = _efs_pages(args, held)
    for y in sorted(pages):
        _print_page(pages[y]["ident"], pages[y]["where"])
    boundaries = _boundaries(cur)
    try:
        aliases, excl = load_names()
    except ValueError as e:
        halt(str(e))
    check_aliases(cur, aliases, boundaries)
    book = NameBook(boundaries, aliases)
    parsed = {}
    for y in sorted(pages):
        body = (pages[y]["page"].get("details") or {}).get("body")
        try:
            resolve_directions(section_names(body), book, excl)
            dirs = resolve_directions(capitalisation_directions(body, y), book,
                                      excl)
            recs = efs_records(pages[y]["page"], book, excl, dirs)
        except ValueError as e:
            halt(f"{e}; nothing stored")
        parsed[y] = recs
        print(f"  {y}: {len(recs)} authorities; capitalisation directions "
              f"for {y}: {', '.join(sorted(dirs)) or 'none'}")
        if recs.excluded:
            print(f"  {y}: excluded (not a lower-tier or unitary authority): "
                  + ", ".join(n for n, _, _ in recs.excluded))
    codes = _flag_set(parsed)
    _, problems = geography.resolve(cur, "12", codes)
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    held_other = {p: r for p, r in _held_records(cur, spec, tips,
                                                 has_ed).items()
                  if p not in parsed}
    try:
        kept, dropped = apply_withdrawn_rule(parsed, held_other)
    except ValueError as e:
        halt(f"{e}; nothing stored")
    rule = WITHDRAWN_ONLY_NOT_SUPPORT
    print(f"rule {rule['name']} (withdrawn-only authorities are not in the "
          f"EFS support list; {rule['decided']}): not stored "
          + (", ".join(f"{c} {y}" for c, y, _ in dropped) or "none"))
    for c in sorted({c for c, _, _ in dropped}):
        print(f"  evidence {c}: {rule['codes'][c]}")
    statements = other_year_statements(parsed)
    for s in statements:
        print(f"  statement on the {s['source_year']} page: {s['name']} "
              f"{s['year']} £{_amount_text(s['amount'])}m: {s['outcome']}"
              + (f" (the {s['year']} page: "
                 f"{_amount_text(s['own_amount']) or 'NULL'})"
                 if s["outcome"] in ("agrees", "differs") else ""))
    dis = disagreements(statements)
    info = {y: {"where": pages[y]["where"], "sha": pages[y]["sha"],
                "rank": pages[y]["rank"]} for y in pages}
    chk = {p: s for p, s in checked.items() if p not in stranded}
    try:
        new, revised, skipped = plan_periods(
            info, ranks, chk, recheck=args.recheck,
            allow_older=args.allow_older_file, only=args.year,
            stranded=stranded)
    except ValueError as e:
        halt(str(e))
    plan.by_period, plan.new, plan.revised, plan.skipped = kept, new, revised, skipped
    for y, reason in sorted(skipped.items()):
        print(f"  {y}: {reason}")
    if not plan.periods and skipped and all(
            r.startswith("older") for r in skipped.values()):
        plan.rank_halt = (
            "older page: every year read is skipped; the pages are older "
            "than what is held for " + ", ".join(sorted(skipped))
            + "; storing them would record an older page. If this is "
            "deliberate, re-run with --allow-older-file")
    held_recs = _held_records(cur, spec, tips, has_ed)
    before = _flag_set(held_recs)
    for p in plan.periods:
        recs = kept[p]
        kind = "new" if p in new else "revised"
        tip = held_recs.get(p) if kind == "revised" else None
        rule_dropped = {c for c, y, _ in dropped if y == p}
        hard, soft, added = efs_period_problems(recs, tip, kind=kind,
                                                rule_dropped=rule_dropped)
        for s in dis.get(p, []):
            soft.append(f"the {s['source_year']} page says {s['name']} "
                        f"{p} is £{_amount_text(s['amount'])}m, the {p} "
                        f"page says {_amount_text(s['own_amount']) or 'NULL'}: "
                        "differs (the year's own page wins)")
        gained = sorted(set(added) - before)
        if added:
            print(f"  {p}: authorities new to the year: {', '.join(added)}"
                  + (f"; MAP: {', '.join(gained)} gain efs_flag" if gained
                     else ""))
        if rule_dropped:
            print(f"  {p}: withdrawn-only rows not stored: "
                  f"{', '.join(sorted(rule_dropped))}")
        plan.hard[p], plan.soft[p] = hard, soft
        if kind == "revised":
            plan.flips[p] = efs_flips(recs, tip)
            t = ranks.get(p)
            cmp_cols = (EFS_VALUES if (tips.get(p, {}).get("label") or "")
                        .startswith("as loaded") or not has_ed
                        else EFS_COMPARED)
            if t is not None and t == pages[p]["rank"] and \
                    _differs(recs, tip, cmp_cols):
                plan.reissue[p] = (f"the page is {rank_text(t)}, the same as "
                                   "the page of the held tip, but the content "
                                   "differs: two pages claim the same release")
            if t is not None and t > pages[p]["rank"]:
                plan.older[p] = (f"--allow-older-file given; {p}'s tip comes "
                                 f"from a page of {rank_text(t)}, this page is "
                                 f"{rank_text(pages[p]['rank'])}")
                print(f"NOTE: {plan.older[p]}")
        plan.info_args[p] = pages[p]
    plan.run_notes.append(
        "rule " + rule["name"] + ": " + (", ".join(
            f"{c} {y}" for c, y, _ in dropped
            if y in plan.periods) or "nothing dropped"))
    plan.before_flags = before
    plan.held_recs = held_recs
    plan.rule_codes = {c for c, _, _ in dropped}
    plan.nothing = not plan.periods
    return plan


def _efs_info(y, pg, notes) -> PeriodInfo:
    rt = rank_text(pg["rank"])
    extra = "".join(f"; {n}" for n in notes if n)
    label = (f"MHCLG Exceptional Financial Support for local authorities for "
             f"{y}; {rt}; {pg['where']} sha256 {pg['sha'][:16]}; {pg['how']}"
             + extra)
    source = f"{pg['file_name']}; EFS {y}; {rt}; {pg['where']}" + extra
    return PeriodInfo(source, label, pg["rank"].date(),
                      ((ledger_source(pg["where"], y, pg["rank"]), pg["sha"]),))


def _prepare_s114(args, cur, writing) -> Plan:
    prof, spec, has_ed, tips, checked, stranded = _prepare_tables(
        cur, "s12_s114", writing)
    plan = Plan("s114", prof, has_ed)
    path = args.s114_file or globals()["S114_FILE"]
    boundaries = _boundaries(cur)
    reg = read_s114(path, boundaries)
    ident = manual_input.file_identity(reg.path)
    where = _file_source(reg.path)
    manual_input.announce_manual("S12 section 114 notices (curated register)",
                                 where, ident)
    print(f"  register_as_at {reg.as_at}; {len(reg.rows)} notices in "
          f"{len(reg.by_period)} years; unevidenced: {len(reg.unevidenced)} "
          f"of {len(reg.rows)}")
    for r in reg.unevidenced:
        print(f"    without evidence: {r['la_name']} {r['lad24cd']} "
              f"{r['notice_date']}")
    for w in reg.warnings:
        print(f"  WARNING: {w}")
    sha = ident["sha256"]
    lsrc = s114_ledger_source(where, reg.as_at)
    ranks = tip_ranks(tips, checked, rank_of=s114_rank)
    periods = sorted(set(reg.by_period) | set(ranks))
    held_recs = _held_records(cur, spec, tips, has_ed)
    if has_ed and args.recheck is None and periods and all(
            (lsrc, sha) in checked.get(p, ()) for p in periods
            if p not in stranded) and not stranded:
        print(f"  the register's (source, sha256) is already in the ledger "
              f"for {', '.join(periods)}; nothing compared (--recheck "
              "YEAR reads it again)")
        plan.nothing = True
        plan.skipped = {p: "unchanged: already in the ledger" for p in periods}
        return plan
    if args.recheck is not None and args.recheck not in ranks:
        halt(f"--recheck {args.recheck}: not a held S.114 year")
    for p in periods:
        t = ranks.get(p)
        if p not in ranks:
            plan.new.append(p)
            continue
        if t is not None and t > reg.as_at and not args.allow_older_file:
            plan.skipped[p] = (f"older: its tip comes from a register as at "
                               f"{t}, this register is as at {reg.as_at}")
            continue
        if p != args.recheck and p not in stranded and \
                (lsrc, sha) in checked.get(p, ()):
            plan.skipped[p] = "unchanged: this register is already in the ledger"
            continue
        plan.revised.append(p)
    for p, reason in sorted(plan.skipped.items()):
        print(f"  {p}: {reason}")
    if not plan.periods and plan.skipped and all(
            r.startswith("older") for r in plan.skipped.values()):
        plan.rank_halt = (f"older register: every year is skipped; the "
                          f"register is as at {reg.as_at}, older than what "
                          f"is held; re-run with --allow-older-file if this "
                          "is deliberate")
    before = _flag_set(held_recs)
    for p in plan.periods:
        recs = reg.by_period.get(p, [])
        kind = "new" if p in plan.new else "revised"
        hard, soft, listed = s114_period_problems(
            recs, held_recs.get(p) if kind == "revised" else None, kind=kind)
        for line in listed:
            print(f"  {p}: {line}")
        gained = sorted({r["lad24cd"] for r in recs} - before)
        if gained:
            print(f"  {p}: MAP: {', '.join(gained)} gain s114_flag")
        plan.hard[p], plan.soft[p] = hard, soft
        if kind == "revised":
            t = ranks.get(p)
            if t is not None and t == reg.as_at and _differs(
                    recs, held_recs[p], S114_COMPARED, S114_KEY):
                plan.reissue[p] = (f"the register is as at {reg.as_at}, the "
                                   "same as the held tip's, but the content "
                                   "differs")
            if t is not None and t > reg.as_at:
                plan.older[p] = (f"--allow-older-file given; {p}'s tip comes "
                                 f"from a register as at {t}")
                print(f"NOTE: {plan.older[p]}")
        plan.info_args[p] = {"where": where, "sha": sha, "as_at": reg.as_at,
                             "lsrc": lsrc, "n": len(reg.rows),
                             "unev": len(reg.unevidenced)}
        plan.by_period[p] = recs
    plan.before_flags = before
    plan.held_recs = held_recs
    plan.rule_codes = set()
    plan.nothing = not plan.periods
    return plan


def _s114_info(p, a, notes) -> PeriodInfo:
    extra = "".join(f"; {n}" for n in notes if n)
    label = (f"S.114 register (curated, research-tier) register_as_at "
             f"{a['as_at']}; {a['where']} sha256 {a['sha'][:16]}; "
             f"{a['n'] - a['unev']} of {a['n']} notices evidenced" + extra)
    source = f"{a['where']}; register_as_at {a['as_at']}" + extra
    return PeriodInfo(source, label, a["as_at"], ((a["lsrc"], a["sha"]),))


def _finalize(plan, args, ack, reissue) -> tuple:
    """(problems {year: [msg]}, infos {year: PeriodInfo}, notes {year: [..]})
    with the acknowledgements applied."""
    problems, infos, notes = {}, {}, {}
    flips_name = getattr(args, "acknowledge_flips", None)
    for p in plan.periods:
        bad = list(plan.hard.get(p, []))
        soft = plan.soft.get(p, [])
        n = [plan.older.get(p, "")]
        if soft and p in ack:
            n.append("ACKNOWLEDGED " + "; ".join(soft))
            print(f"  {p}: ACKNOWLEDGED (--acknowledge): {'; '.join(soft)}")
        elif soft:
            bad += [x + f"; read the preview, then --acknowledge {p}"
                    for x in soft]
        fl = flip_problems(flips_name, p, plan.flips.get(p, {}))
        bad += fl
        if plan.flips.get(p) and not fl:
            a = ACKNOWLEDGED_FLIPS[flips_name]
            n.append(f"ACKNOWLEDGED FLIPS {flips_name} ({a['decided']})")
            print(f"  {p}: ACKNOWLEDGED (--acknowledge-flips {flips_name}): "
                  f"{a['why']}")
        if p in plan.reissue:
            if p in reissue:
                msg = (f"--accept-reissue {p}: {plan.reissue[p]} (a publisher "
                       "reissue keeping its date)")
                n.append(msg)
                print(f"  {p}: ACCEPTED REISSUE: {msg}")
            else:
                bad.append(plan.reissue[p] + "; a publisher reissue keeps its "
                           f"date: read the change note, then "
                           f"--accept-reissue {p}")
        if bad:
            problems[p] = bad
            continue
        notes[p] = [x for x in n if x]
        a = plan.info_args[p]
        infos[p] = (_efs_info(p, a, notes[p]) if plan.part == "efs"
                    else _s114_info(p, a, notes[p]))
    return problems, infos, notes


def _flag_guard(plan, ok) -> None:
    """The map stop condition: after storing the years in `ok` (and before
    refresh-latest), the authorities with any row may lose only the
    withdrawn-only rule's (EFS) or named S.114 removals."""
    after = {p: plan.by_period[p] for p in ok}
    for p, recs in plan.held_recs.items():
        after.setdefault(p, recs)
    lost = plan.before_flags - _flag_set(after)
    allowed = set(plan.rule_codes)
    allowed |= {c for c, _ in S114_REMOVALS} if plan.part == "s114" else set()
    bad = sorted(lost - allowed)
    if bad:
        halt(f"MAP STOP: storing {ok} would leave {bad} with no row at all "
             "(their flag would go) outside the named rules; nothing stored")
    if lost:
        print(f"MAP: after refresh-latest {sorted(lost)} have no row "
              f"({'withdrawn-only rule' if plan.part == 'efs' else 'named removal'})")


def _execute(plan, args, conn, cur, problems, infos, writing) -> int:
    started = datetime.now(timezone.utc)
    prof = plan.prof
    for p, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {p}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [p for p in plan.periods if p not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok:
        _flag_guard(plan, ok)
    if ok and plan.has_ed:
        def apply(c, prof_, per, rs, *, fetched_on, source_file=None):
            return _late("apply_part")(c, prof_, per, rs,
                                       fetched_on=fetched_on, info=infos[per])

        def compare(c, prof_, per, rs, against):
            return pe.compare_period(c, compare_profile(c, prof_, per), per,
                                     rs, against)
        rc = pe.load_periods(cur, prof, ok, lambda per: plan.by_period[per],
                             date.today(), args.commit,
                             simulate=args.simulate, against="editions",
                             stats=stats, compare=compare, apply=apply)
    elif ok:
        values = EFS_VALUES if plan.part == "efs" else S114_VALUES
        live_prof = dataclasses.replace(prof, value_cols=values)
        rc = pe.load_periods(cur, live_prof, ok,
                             lambda per: plan.by_period[per], date.today(),
                             False, against="live")
        print("  (no editions table yet: preview against live only, on the "
              "live values)")
    if problems:
        print(f"REJECTED {len(problems)} year(s), nothing stored for them: "
              f"{', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit and (stats["periods"] or problems or rc):
        stored = live = 0  # rows written by this run; not a source value
        for p in stats["periods"]:
            k = stats["kinds"][p]
            if k in ("new", "revised"):
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
                       f"{not_tried or 'none'}; a year that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        what = "EFS year pages" if plan.part == "efs" else "S.114 register"
        notes = (partial + f"{what}: "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(plan.skipped)}" if plan.skipped else "")
                 + "".join(f"; {r}" for r in plan.run_notes)
                 + "".join(f"; {p}: {'; '.join(plan.notes.get(p, []))}"
                           for p in stats["periods"] if plan.notes.get(p))
                 + f". Rows stored: {stored} edition rows, {live} live rows.")
        log_run(cur, stored + live, notes, started)
        conn.commit()
        print("pipeline_run_log row written"
              + (" (partial run: see its notes)" if rc else ""))
    return rc


def cmd_load(args) -> int:
    parts = [args.only] if args.only else ["efs", "s114"]
    if (args.page_file or args.no_page or args.year) and "efs" not in parts:
        halt("--page-file, --no-page and --year are for the EFS pages; drop "
             "--only s114")
    if args.s114_file and "s114" not in parts:
        halt("--s114-file is for the S.114 register; drop --only efs")
    if args.no_page and not args.page_file:
        halt("--no-page is only for --page-file (saved page JSON read "
             "without the collection)")
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            plans = []
            for part in parts:
                print(f"=== {part.upper()} ===")
                plan = (_prepare_efs(args, cur, writing) if part == "efs"
                        else _prepare_s114(args, cur, writing))
                plans.append(plan)
            for plan in plans:
                if plan.rank_halt:
                    halt(plan.rank_halt)
            ack = set(args.acknowledge or ())
            reissue = set(args.accept_reissue or ())
            considered = set().union(*(set(p.periods) for p in plans))
            stray = sorted((ack | reissue) - considered)
            if stray:
                halt(f"--acknowledge/--accept-reissue {', '.join(stray)}: not "
                     f"years this run compares or stores "
                     f"({', '.join(sorted(considered)) or 'none'}); nothing "
                     "stored")
            if args.acknowledge_flips and not any(
                    p.part == "efs" and any(p.flips.values()) for p in plans):
                halt(f"--acknowledge-flips {args.acknowledge_flips}: no amount "
                     "goes to or from NULL in this run; nothing stored")
            rc = 0
            for plan in plans:
                print(f"=== {plan.part.upper()}: planned new "
                      f"{plan.new or 'none'}; compare {plan.revised or 'none'}"
                      f"; skipped {sorted(plan.skipped) or 'none'} ===")
                if plan.nothing:
                    print("nothing to do")
                    continue
                problems, infos, notes = _finalize(plan, args, ack, reissue)
                plan.notes = notes
                rc = max(rc, _execute(plan, args, conn, cur, problems, infos,
                                      writing))
        return _end(conn, args, writing, rc)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# One-off: migrate the held tables
# ---------------------------------------------------------------------------

def live_state(cur, spec) -> tuple:
    """(rows, survey hash, distinct loaded_at, ((year, rows), ...)) of a
    live table. The survey hash: md5 of the columns but loaded_at, each
    coalesce(col::text,'~'), '|'-joined, rows ordered by financial_year,
    lad24cd (EFS) or lad24cd, notice_date (S.114), joined by LF."""
    if _is_s114(spec):
        cols = ("lad24cd", "notice_date", "financial_year", "reason",
                "date_confirmed", "source", "attribution", "successor_codes",
                "attribution_note")
        order = "lad24cd, notice_date"
    else:
        cols = ("lad24cd", "financial_year", "amount_m", "status", "hra_only",
                "source")
        order = "financial_year, lad24cd"
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in cols)
    t = spec.live_table
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"{order})), COUNT(DISTINCT loaded_at) FROM public.{t}")
    n, h, nl = cur.fetchone()
    cur.execute(f"SELECT financial_year, COUNT(*) FROM public.{t} GROUP BY 1 "
                "ORDER BY 1")
    return (n, h, nl, tuple((pe._p(p), c) for p, c in cur.fetchall()))


def _read_legacy_s114(path) -> dict:
    """{(lad24cd, date): row} of the six-column list the n8n load read;
    halts on another header or a duplicate key."""
    text = Path(path).read_text(encoding="utf-8-sig")
    rd = csv.reader(io.StringIO(text))
    header = tuple(c.strip() for c in next(rd, []))
    if header != LEGACY_S114_HEADER:
        halt(f"{Path(path).name}: header {header}, expected "
             f"{LEGACY_S114_HEADER}; nothing stored")
    out = {}
    for rec in rd:
        if not rec or all(not c.strip() for c in rec):
            continue
        row = dict(zip(header, (c.strip() for c in rec)))
        k = (row["lad24cd"], date.fromisoformat(row["notice_date"]))
        if k in out:
            halt(f"{Path(path).name}: {k} twice; nothing stored")
        out[k] = row
    return out


def migrate_legacy(cur, *, write) -> dict:
    """One-off. Preconditions: on write both editions tables and ledgers
    exist and are empty (in a preview they may be absent; present, they must
    be empty); each live table as surveyed (LEGACY_EFS, LEGACY_S114); the held
    files' sha256 as listed (LEGACY_PAGES, LEGACY_S114_FILE).

    EFS proof (any failure halts): each held year page, read by this loader
    without the withdrawn-only rule, lists exactly the authorities live holds
    for that year; every authority is printed with the held and the page's
    amount, status and hra_only (the differences are what the first load
    stores as the next edition). A year whose page reproduces live on all
    three gets a ledger row for the page (outcome 'unchanged', edition 1).

    S.114 proof: the held six-column list reproduces every live row on its
    six columns, live source is the n8n text, and attribution is 'direct'
    (no successors, no note) exactly where the code is in la_boundaries,
    else 'predecessor' with successors and a note (gate 14). A ledger row
    per year for the list.

    Edition 1 'as loaded' of every year of both tables is live as held (EFS
    cell_text and page_updated_at NULL; S.114 evidence NULL: every notice is
    unevidenced). Live untouched. Nothing commits here."""
    g = globals()
    pe_ = profile("s12_efs")
    ps_ = profile("s12_s114")
    ready = {}
    for prof in (pe_, ps_):
        for t in (prof.spec.editions_table, pe.file_checks_table(prof)):
            ready[t] = table_exists(cur, t)
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
    for prof, want in ((pe_, g["LEGACY_EFS"]), (ps_, g["LEGACY_S114"])):
        state = tuple(live_state(cur, prof.spec))
        if state != tuple(want):
            halt(f"{prof.spec.live_table} is not as surveyed: (rows, hash, "
                 f"loaded_at values, years) {state}, expected {tuple(want)}; "
                 "nothing stored")
    boundaries = _boundaries(cur)
    codes = set(boundaries.values())
    try:
        aliases, excl = load_names()
    except ValueError as e:
        halt(str(e))
    check_aliases(cur, aliases, boundaries)
    book = NameBook(boundaries, aliases)
    plan = {"efs": {}, "s114": {}, "ledger_efs": {}, "ledger_s114": None}
    # --- EFS ---------------------------------------------------------------
    espec = pe_.spec
    live_years = sorted({p for p, _ in g["LEGACY_EFS"][3]})
    pages = g["LEGACY_PAGES"]
    if sorted(pages) != live_years:
        halt(f"held pages for {sorted(pages)}, live years {live_years}; "
             "nothing stored")
    print("EFS proof: each held page read by this loader (no withdrawn-only "
          "rule) against live; per authority: held amount status hra_only | "
          "page amount status hra_only")
    parsed = {}
    for y in live_years:
        f = pages[y]
        sha = content_sha256(f["path"])
        if sha != f["sha256"]:
            halt(f"{Path(f['path']).name}: sha256 {sha[:16]}, expected "
                 f"{f['sha256'][:16]} (the held page); nothing stored")
        pg = json.loads(Path(f["path"]).read_text(encoding="utf-8"))
        try:
            ident = page_identity(pg)
            if ident["year"] != y:
                raise ValueError(f"the page is for {ident['year']}, not {y}")
            body = (pg.get("details") or {}).get("body")
            resolve_directions(section_names(body), book, excl)
            dirs = resolve_directions(capitalisation_directions(body, y),
                                      book, excl)
            recs = efs_records(pg, book, excl, dirs)
        except ValueError as e:
            halt(f"proof failed: {Path(f['path']).name}: {e}; nothing stored")
        parsed[y] = recs
        held = {r["lad24cd"]: r for r in records(cur, espec, y)}
        got = {r["lad24cd"]: r for r in recs}
        if set(held) != set(got):
            halt(f"proof failed: {y}: held only {sorted(set(held) - set(got))}"
                 f", page only {sorted(set(got) - set(held))}; nothing "
                 "stored")
        diffs = 0
        print(f"  {y}: {ident['title']}; {rank_text(ident['rank'])}; "
              f"{len(held)} authorities; excluded "
              + (", ".join(n for n, _, _ in recs.excluded) or "none"))
        for k in sorted(held):
            h, r = held[k], got[k]
            cols = [c for c in EFS_VALUES if not _same(h[c], r[c])]
            diffs += bool(cols)
            print(f"    {k} {r['name']}: {_show(h['amount_m'])} {h['status']} "
                  f"{_show(h['hra_only'])} | {_show(r['amount_m'])} "
                  f"{r['status']} {_show(r['hra_only'])}"
                  + (f"  [{', '.join(cols)}]" if cols else "  ="))
        if not diffs:
            plan["ledger_efs"][y] = (ledger_source(_file_source(f["path"]), y,
                                                   ident["rank"]),
                                     page_sha(pg))
        print(f"  {y}: {diffs} authorit(ies) differ"
              + ("; the page reproduces the year: ledger row written"
                 if not diffs else "; load stores the page as edition 2"))
    try:
        _, dropped = apply_withdrawn_rule(parsed)
        print("  after migration, load leaves out under "
              f"{WITHDRAWN_ONLY_NOT_SUPPORT['name']}: "
              + (", ".join(f"{c} {y}" for c, y, _ in dropped) or "none"))
    except ValueError as e:
        print(f"  WARNING: {e}")
    statements = other_year_statements(parsed)
    for y, sts in disagreements(statements).items():
        for s in sts:
            print(f"  statement disagreeing with the {y} page: the "
                  f"{s['source_year']} page says {s['name']} "
                  f"£{_amount_text(s['amount'])}m, the {y} page "
                  f"{_amount_text(s['own_amount']) or 'NULL'}")
    for y in live_years:
        recs = records(cur, espec, y)
        for r in recs:
            r["cell_text"] = None
            r["page_updated_at"] = None
        cur.execute(f"SELECT DISTINCT (loaded_at AT TIME ZONE 'UTC')::date "
                    f"FROM public.{espec.live_table} WHERE financial_year = %s",
                    (y,))
        pub = [r[0] for r in cur.fetchall()]
        plan["efs"][y] = {"records": recs, "published": max(pub),
                          "source_file": EFS_AS_LOADED_SOURCE,
                          "content_sha": rows_content_sha(recs)}
    # --- S.114 -------------------------------------------------------------
    sspec = ps_.spec
    lf = g["LEGACY_S114_FILE"]
    sha = content_sha256(lf["path"])
    if sha != lf["sha256"]:
        halt(f"{Path(lf['path']).name}: sha256 {sha[:16]}, expected "
             f"{lf['sha256'][:16]} (the held S.114 list); nothing stored")
    rows = _read_legacy_s114(lf["path"])
    s_years = sorted({p for p, _ in g["LEGACY_S114"][3]})
    cur.execute(f"SELECT lad24cd, notice_date, source FROM "
                f"public.{sspec.live_table}")
    srcs = {(c, d): s for c, d, s in cur.fetchall()}
    bad = []
    print("S.114 proof: the held list against live (six columns; "
          "attribution by the gate 14 rule)")
    for y in s_years:
        held = {(r["lad24cd"], r["notice_date"]): r
                for r in records(cur, sspec, y)}
        for k, r in sorted(held.items()):
            f = rows.get(k)
            if f is None or (f["financial_year"], f["reason"],
                             f["date_confirmed"]) != (
                    r["financial_year"], r["reason"], r["date_confirmed"]):
                bad.append(f"{y} {k[0]} {k[1]}: held {r['reason']!r} "
                           f"{r['date_confirmed']!r}, list "
                           f"{(f or {}).get('reason')!r}")
                continue
            if srcs[k] != N8N_S114_SOURCE:
                bad.append(f"{y} {k[0]} {k[1]}: source {srcs[k]!r}")
            direct = k[0] in codes
            if direct != (r["attribution"] == "direct") or (
                    direct and (r["successor_codes"] or r["attribution_note"]))\
                    or (not direct and not (r["successor_codes"]
                                            and r["attribution_note"])):
                bad.append(f"{y} {k[0]} {k[1]}: attribution "
                           f"{r['attribution']!r} breaks the gate 14 rule")
            if fy_of(k[1]) != r["financial_year"]:
                print(f"  NOTE: {k[0]} {k[1]}: financial_year "
                      f"{r['financial_year']} is not the April-March year of "
                      f"its date ({fy_of(k[1])}); held as it is")
            print(f"    {y} {k[0]} {k[1]} {r['reason']} "
                  f"({r['date_confirmed']}; {r['attribution']}): =")
    held_keys = {(c, d) for c, d in srcs}
    extra = sorted(set(rows) - held_keys)
    if extra:
        bad.append(f"in the list, not held: {extra}")
    if bad:
        halt(f"S.114 proof failed: {len(bad)} difference(s): "
             + "; ".join(bad[:6]) + "; nothing stored")
    n_all = len(held_keys)
    print(f"  the held list reproduces all {n_all} notices; unevidenced: "
          f"{n_all} of {n_all} (edition 1 carries no evidence; the register "
          "gets it as a later, listed step)")
    lsrc = s114_ledger_source(_file_source(lf["path"]), None)
    plan["ledger_s114"] = (lsrc, sha)
    for y in s_years:
        recs = records(cur, sspec, y)
        cur.execute(f"SELECT DISTINCT (loaded_at AT TIME ZONE 'UTC')::date "
                    f"FROM public.{sspec.live_table} WHERE financial_year = %s",
                    (y,))
        pub = [r[0] for r in cur.fetchall()]
        plan["s114"][y] = {"records": recs, "published": max(pub),
                           "source_file": S114_AS_LOADED_SOURCE.format(
                               Path(lf["path"]).name, sha[:16]),
                           "content_sha": s114_content_sha(recs)}
    plan["rows"] = (sum(len(v["records"]) for v in plan["efs"].values())
                    + sum(len(v["records"]) for v in plan["s114"].values()))
    for part, prof in (("efs", pe_), ("s114", ps_)):
        for y, v in plan[part].items():
            print(f"  {prof.spec.editions_table} {y}: edition 1 as loaded, "
                  f"{len(v['records'])} rows, published {v['published']}; "
                  f"content sha256 {v['content_sha']}")
    if not write:
        return plan
    cur.execute("SAVEPOINT s12_migrate")
    for part, prof in (("efs", pe_), ("s114", ps_)):
        spec = prof.spec
        for y, v in plan[part].items():
            ed = core.insert_edition(
                cur, spec, v["records"], y, release_label=core.AS_LOADED_LABEL,
                published_date=v["published"], source_file=v["source_file"],
                source_sha256=v["content_sha"], supersedes=None, strict=True)
            if ed != 1 or core.rows_differing(cur, spec, y, ed):
                halt(f"{spec.editions_table} {y}: edition 1 does not equal "
                     "live; rolled back")
            if part == "efs" and y in plan["ledger_efs"]:
                src, psha = plan["ledger_efs"][y]
                pe.record_file_check(cur, prof, y, src, psha, "unchanged", 1)
            if part == "s114":
                pe.record_file_check(cur, prof, y, plan["ledger_s114"][0],
                                     plan["ledger_s114"][1], "unchanged", 1)
    cur.execute("RELEASE SAVEPOINT s12_migrate")
    return plan


def cmd_migrate_legacy(args) -> int:
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, write=writing)
            if args.commit:
                log_run(cur, plan["rows"], f"migrate-legacy: edition 1 as "
                        f"loaded for la_efs_support "
                        f"{', '.join(plan['efs'])} and la_s114_notices "
                        f"{', '.join(plan['s114'])} ({plan['rows']} rows) "
                        "from the live tables; proof against the held pages "
                        "and the held S.114 list; ledger rows for "
                        f"{', '.join(plan['ledger_efs']) or 'no EFS page'} "
                        "and the S.114 list. Live untouched.", started)
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
    if isinstance(spec, str):
        spec = profile(spec).spec
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
    sha = (s114_content_sha(recs) if _is_s114(spec)
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
    spec = profile(args.spec).spec
    try:
        with conn.cursor() as cur:
            if not table_exists(cur, spec.editions_table):
                halt(f"{spec.editions_table} does not exist")
            new = restore_edition(cur, spec, args.period, args.edition)
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
        description="S12 MHCLG Exceptional Financial Support and S.114 "
        "notices: editions loader on the period-editions engine. There is no "
        "sync-new; migrate-legacy records the held years once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create both editions tables, their "
                       "triggers and the file-check ledgers (preview by "
                       "default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read the EFS year pages and the S.114 "
                       "register, compare and store each year (preview by "
                       "default)")
    p.add_argument("--only", choices=sorted(PARTS))
    p.add_argument("--year", type=_year_arg, metavar="YYYY-YY",
                   help="EFS: compare and store only this year (every page "
                   "is still read, for the statements about other years)")
    p.add_argument("--page-file", action="append", metavar="PATH",
                   help="EFS: a saved content-API JSON of a year page "
                   "(repeatable; under data/raw or data/reference)")
    p.add_argument("--no-page", action="store_true",
                   help="with --page-file: do not read the collection "
                   "(recorded as such)")
    p.add_argument("--s114-file", metavar="PATH",
                   help="the S.114 register (default "
                   "data/reference/la_s114_notices.csv)")
    p.add_argument("--recheck", type=_year_arg, metavar="YYYY-YY",
                   help="compare this held year again although its input is "
                   "in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a year whose tip comes from a "
                   "newer page or register (logged)")
    p.add_argument("--acknowledge", action="append", type=_year_arg,
                   metavar="YYYY-YY",
                   help="release a year's soft stop conditions after reading "
                   "the preview (repeatable; logged; never a partial page or "
                   "a removed notice)")
    p.add_argument("--accept-reissue", action="append", type=_year_arg,
                   metavar="YYYY-YY",
                   help="store a year whose page or register has the held "
                   "rank but different content (repeatable; logged)")
    p.add_argument("--acknowledge-flips", metavar="NAME",
                   choices=sorted(ACKNOWLEDGED_FLIPS),
                   help="release exactly the amounts going to or from NULL "
                   "that a named entry lists (ACKNOWLEDGED_FLIPS; logged)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy each year's latest "
                       "edition into the live tables, loaded_at included "
                       "(preview by default)")
    p.add_argument("--accept-drift", action="append", type=_year_arg,
                   metavar="YYYY-YY",
                   help="overwrite this year although its live rows equal no "
                   "stored edition (repeatable)")
    p.add_argument("--accept-key-changes", action="append", type=_year_arg,
                   metavar="YYYY-YY",
                   help="apply this year's keys added and removed "
                   "(repeatable)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for both tables, with the proof (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("spec", choices=sorted(SPECS))
    p.add_argument("period", type=_year_arg, metavar="YYYY-YY")
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
