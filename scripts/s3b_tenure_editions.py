"""S3b (ONS Census 2021 TS054 Tenure, through NOMIS NM_2072_1): edition
history and loader on the period-editions engine, with a single-vintage live
table.

The live table la_tenure_2021 keeps its name, key (lad24cd) and columns and
holds one census (no reader in W1, the export or the map). The census year
is the engine period (census_year, integer in the database, the string
'2021' in the engine; it sorts, rule 8). This module owns the editions table
and file-check ledger, append-only:

    SPEC     la_tenure_2021_editions               period census_year
    ledger   la_tenure_2021_editions_file_checks

An edition is what one NOMIS reply says about its census: per authority the
nine published counts (the publisher's total and its eight parts). The two
generated live columns (social_rented_total, private_rented_total) are never
written; Postgres computes them from the parts. A reissue of the held census
(a later LastUpdated with different content, or the same LastUpdated with
different content) stops unless --accept-reissue YEAR: Census counts are not
expected to change. An accepted reissue is the next edition and reaches live
only through refresh-latest. Another census (2031, say) is stored as editions
only and never enters live through a load (EditionSpec.live_single_period);
it needs its dataset declared in DATASETS deliberately, with the evidence of
its own definition; until then its definition halts, which is the intended
stop.

Rule 1 (blanks and zeros): every cell is an integer (int_cell); a blank or
text halts and is never read as 0. The publisher's own total
('Total: All households') is stored, and the eight parts must sum to it on
every authority or the load halts (checks). The total is never built from
the parts: the n8n code did that, defaulting a missing part to zero, and
re-based the four 2023 unitaries from their predecessors (wrong by up to
75,940 households; docs/decisions/2026-08-20-s3b-tenure-rebasing-error.md).

History (so the wording here stays truthful): the n8n workflow loaded the
table on 2026-04-01 (pipeline_run_log 29; its fetch node calls the ONS
beta API, but its node name, the run-log note and the 292 rows' source say
Nomis NM_2072_1, so the route is not established); on
2026-08-20 the four 2023 unitaries were reloaded from NOMIS NM_2072_1 by
direct SQL (the decision note). No held value is affected: every held value
equals the held NOMIS reply read again (data/raw/s3b_tenure). migrate-legacy
(one-off) records the held census as edition 1 "as loaded", with that proof
on every stored count (296 x 9 = 2,664). The live source column keeps its
two held variants (292 rows of 2026-04-01, 4 of 2026-08-20) as provenance:
it is never compared, and a refresh writes it only on the rows it changes.

Identity (rule 3), from the source itself on every path:
  - the NOMIS dataset definition (def.sdmx.json): id NM_2072_1, name
    'TS054 - Tenure', mnemonic c2021ts054 (a declared dataset in DATASETS;
    any other halts), FirstReleased and LastUpdated. LastUpdated is the
    rank, never a file name or a download date.
  - the category codes, looked up by exact name from the dataset's own
    codelist and never assumed: 1001 to 1004, 9996 and 9997 are the
    publisher's aggregates (isDerived, with children), so a part whose code
    is an aggregate halts (category_codes).
  - the data CSV: every row's GEOGRAPHY_TYPE exactly 'local authorities:
    district / unitary (as of April 2023)' (TYPE424; a TYPE on 2021
    boundaries would give the predecessor districts), DATE_NAME the census
    year, RECORD_COUNT equal to the rows read and RECORD_OFFSET 0..n-1 (a
    truncated reply halts), each English authority with the nine categories
    exactly once.
The CSV and definition bytes are API output (the definition carries the
time it was prepared), so a file's bytes are not a release identity: the
definition plus the content is. The content sha (rows_content_sha: lad24cd
and the nine counts) decides 'unchanged' and is what the ledger records.

Discovery: the NOMIS dataset search (*TS054*) must list the declared
dataset under its declared name and mnemonic; a TS054 dataset that is not
declared (a new census) halts, naming what was seen.

Geography (rule 4): England (E06-E09) is stored; Wales (W06) is read and
ignored; any other code halts. geography.resolve('3b', codes) (source 3b is
declared 'old': NOMIS returns E08000016/19 for TYPE424; an E08000038 or
E08000039 halts), then each code's canonical form must be in la_boundaries.

Stop conditions (in load; a stopped census is REJECTED: nothing stored, no
ledger row, exit 1): a reissue that drops or adds an authority (PARTIAL; no
flag releases it); a census other than 2021 into an empty live table,
new or already held as editions only (no flag releases it); different content for a held census (--accept-reissue
YEAR); an older definition (its LastUpdated before the held tip's, or a
census before the newest held) halts unless --allow-older-file. Each
acceptance is written to the edition's label and the run log.

Subcommands (every writing command previews by default; --commit and
--simulate are mutually exclusive; --simulate runs the --commit path and
always rolls back). There is no sync-new: migrate-legacy records the held
census.
    python scripts/s3b_tenure_editions.py ddl [--commit | --simulate]
    python scripts/s3b_tenure_editions.py status
    python scripts/s3b_tenure_editions.py load [--file CSV --def JSON]
        [--recheck YEAR] [--allow-older-file] [--accept-reissue YEAR ...]
        [--commit | --simulate]
        # without --file: the definition and the CSV are downloaded to
        # data/raw/s3b_tenure/ with the date (also in a preview; a preview
        # writes nothing to the database)
    python scripts/s3b_tenure_editions.py refresh-latest
        [--accept-drift YEAR] [--commit | --simulate]
    python scripts/s3b_tenure_editions.py migrate-legacy --file CSV
        --def JSON [--commit | --simulate]
    python scripts/s3b_tenure_editions.py restore-edition YEAR N
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
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
import geography  # noqa: E402
import period_editions as pe  # noqa: E402
from editions_core import halt  # noqa: E402

RUN_AGENT = "s3b_tenure_editions"
RUN_SOURCE = "3b"
LIVE_MISSING = pe.LIVE_MISSING
HELD_NOT_LIVE = pe.HELD_NOT_LIVE

NOMIS = "https://www.nomisweb.co.uk/api/v01/dataset"
REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s3b_tenure"     # git-ignored
USER_AGENT = ("ucws-pipeline S3b loader (read-only download of the public "
              "ONS Census 2021 TS054 tenure dataset from NOMIS)")

# ---------------------------------------------------------------------------
# Columns (live types from information_schema, checked 2026-10-10:
# lad24cd character varying NOT NULL (PK), the nine counts below integer
# (nullable; 0 NULL held), social_rented_total and private_rented_total
# GENERATED ALWAYS (never written), census_year integer NOT NULL DEFAULT
# 2021, source text, loaded_at timestamptz DEFAULT now()). The editions table
# holds every count NOT NULL: a blank halts, never stored.
# ---------------------------------------------------------------------------

KEY = "lad24cd"
PERIOD = "census_year"
# (live column, the publisher's category name, exactly as the codelist and
# the CSV write it), in codelist order
CATEGORIES = (
    ("total_households", "Total: All households"),
    ("owned_outright", "Owned: Owns outright"),
    ("owned_mortgage", "Owned: Owns with a mortgage or loan"),
    ("shared_ownership", "Shared ownership: Shared ownership"),
    ("social_rented_council",
     "Social rented: Rents from council or Local Authority"),
    ("social_rented_other", "Social rented: Other social rented"),
    ("private_rented_landlord",
     "Private rented: Private landlord or letting agency"),
    ("private_rented_other", "Private rented: Other private rented"),
    ("lives_rent_free", "Lives rent free"),
)
TOTAL = "total_households"
VALUES = tuple(c for c, _ in CATEGORIES)
PARTS = tuple(c for c in VALUES if c != TOTAL)
VALUE_TYPES = tuple((c, "integer NOT NULL") for c in VALUES)
GENERATED = ("social_rented_total", "private_rented_total")

# ---------------------------------------------------------------------------
# The datasets, one per census, declared with the evidence of their own
# definition. 2021: NOMIS NM_2072_1 definition and codelist read 2026-10-10
# (data/raw/s3b_tenure/NM_2072_1_def_2026-10-10.json; FirstReleased and
# LastUpdated 2023-01-05 09:30:00). Another census is added here
# deliberately; until then its definition halts.
# ---------------------------------------------------------------------------

DATASETS = {
    "2021": {"id": "NM_2072_1", "name": "TS054 - Tenure",
             "mnemonic": "c2021ts054", "census_year": "2021",
             "geography": "TYPE424",
             "geography_type": "local authorities: district / unitary (as "
                               "of April 2023)",
             "dimension": "c2021_tenure_9"},
}
MEASURES = "20100"      # NOMIS: the value (not a percentage)
# the one census the live table holds (its name)
LIVE_VINTAGE = "2021"
EXPECTED_AREAS = 296
DISTRICT_RE = re.compile(r"E0[6-9][0-9]{6}")
WELSH_RE = re.compile(r"W06[0-9]{6}")
YEAR_RE = re.compile(r"[0-9]{4}")
TS054_MNEMONIC_RE = re.compile(r"c[0-9]{4}ts054")
PARTIAL = "PARTIAL FILE"    # problems no flag releases
_TS_FMT = "%Y-%m-%d %H:%M:%S"
_TS = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}"

# ---------------------------------------------------------------------------
# The held state (migrate-legacy preconditions), surveyed 2026-10-10
# ---------------------------------------------------------------------------

# (rows, the survey hash: md5 of lad24cd|the nine counts|census_year|source,
# NULL as '~', ordered by census_year, lad24cd; see live_state)
LEGACY_LIVE = (296, "16c5d37b8190e40d" "ac1b1c8a2919eb78")
# the live loaded_at values (UTC) and their row counts
LEGACY_LOADED = {"2026-04-01 01:55:08.514719+00": 292,
                 "2026-08-20 14:20:00.090729+00": 4}
# the held NOMIS reply (sha256 split so the credential scan does not flag
# it). The content sha (rows_content_sha) is what identifies it: the bytes
# are API output. The URL is the data URL a load builds (data_url of the
# codelist's codes 0..8), which the ledger row names so that an API load
# finds the content checked.
LEGACY_FILE = {
    "name": "NM_2072_1_TYPE424_2026-10-10.csv",
    "bytes_sha256": "80b690076f2d920329bf424ba6183168"
                    "c3b39ee4ac4971b455fa05d1101df473",
    "content_sha256": "050605c5a6f22c3cdb12ed0ec33c0c37"
                      "8b409cc344c9cd01aa4f9e8be24b3dde",
    "url": None,    # filled below from data_url
}
# the held definition's LastUpdated (the rank of the held census before the
# editions exist)
LEGACY_RANK = datetime(2023, 1, 5, 9, 30)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _json(x):
    if isinstance(x, (str, bytes)):
        return json.loads(x)
    return x if x is not None else {}


def _ann(node) -> dict:
    """{annotationtitle: annotationtext} of a NOMIS node."""
    out = {}
    for a in ((node.get("annotations") or {}).get("annotation") or []):
        out[str(a.get("annotationtitle"))] = a.get("annotationtext")
    return out


def _value(x):
    return x.get("value") if isinstance(x, dict) else x


def _when(text, where) -> datetime:
    s = str(text or "").strip()
    try:
        return datetime.strptime(s, _TS_FMT)
    except ValueError:
        raise ValueError(f"{where}: {s!r} is not a NOMIS date and time "
                         "(YYYY-MM-DD HH:MM:SS)") from None


def rank_text(rank) -> str:
    """'NOMIS LastUpdated 2023-01-05 09:30:00' (the time as NOMIS publishes
    it, with no time zone)."""
    return (f"NOMIS LastUpdated {rank.strftime(_TS_FMT)}" if rank
            else "no NOMIS LastUpdated")


def search_url() -> str:
    return f"{NOMIS}/def.sdmx.json?search=*TS054*"


def def_url(dataset) -> str:
    return f"{NOMIS}/{dataset['id']}.def.sdmx.json"


def codelist_url(dataset) -> str:
    return f"{NOMIS}/{dataset['id']}/{dataset['dimension']}.def.sdmx.json"


def select_cols(dataset) -> tuple:
    return ("date_name", "geography_name", "geography_code",
            "geography_type", f"{dataset['dimension']}_name", "obs_value",
            "record_offset", "record_count")


def csv_header(dataset) -> list:
    return [c.upper() for c in select_cols(dataset)]


# ---------------------------------------------------------------------------
# The definition, the codelist, discovery, the data URL
# ---------------------------------------------------------------------------

def _keyfamilies(d) -> list:
    return list((((d.get("structure") or {}).get("keyfamilies") or {})
                 .get("keyfamily")) or [])


def _declared() -> str:
    return "; ".join(f"{y}: {ds['id']} {ds['name']!r} ({ds['mnemonic']})"
                     for y, ds in sorted(DATASETS.items()))


def dataset_def(def_json) -> dict:
    """The facts of a NOMIS dataset definition (def.sdmx.json; a dict, or
    its text): {id, name, mnemonic, census_year, dataset (the DATASETS
    entry), first_released, last_updated, rank (= last_updated)}. ValueError
    naming what was seen when it holds other than one dataset, when the
    dataset is not one declared in DATASETS (id, name and mnemonic all
    equal), or when LastUpdated or FirstReleased is absent or not a date:
    the rank is never taken from a file name or a download date."""
    d = _json(def_json)
    kfs = _keyfamilies(d)
    if len(kfs) != 1:
        raise ValueError(f"the definition holds {len(kfs)} datasets "
                         f"({[k.get('id') for k in kfs]}), expected one")
    k = kfs[0]
    ann = _ann(k)
    did, name, mn = k.get("id"), _value(k.get("name")), ann.get("Mnemonic")
    hits = [y for y, ds in DATASETS.items()
            if (ds["id"], ds["name"], ds["mnemonic"]) == (did, name, mn)]
    if len(hits) != 1:
        raise ValueError(f"the definition is {did} {name!r} (mnemonic "
                         f"{mn!r}), not a declared dataset ({_declared()}). "
                         "Another dataset halts; a new census is declared "
                         "in DATASETS deliberately, with its evidence")
    if ann.get("LastUpdated") is None or ann.get("FirstReleased") is None:
        raise ValueError(f"{did}: the definition has no LastUpdated or "
                         "FirstReleased annotation; the rank (LastUpdated) "
                         "cannot be read from the definition itself")
    lu = _when(ann["LastUpdated"], f"{did} LastUpdated")
    fr = _when(ann["FirstReleased"], f"{did} FirstReleased")
    ds = DATASETS[hits[0]]
    return {"id": did, "name": name, "mnemonic": mn,
            "census_year": ds["census_year"], "dataset": ds,
            "first_released": fr, "last_updated": lu, "rank": lu}


def discover(search_json, dataset) -> list:
    """Lines saying what the NOMIS search (*TS054*) listed. ValueError,
    listing every dataset seen, unless the declared dataset is listed once
    under its declared name and mnemonic, or when a TS054 dataset (name
    'TS054...' or mnemonic c<yyyy>ts054) is listed that DATASETS does not
    declare (a new census: never passed over)."""
    seen = []
    for k in _keyfamilies(_json(search_json)):
        a = _ann(k)
        seen.append((k.get("id"), _value(k.get("name")), a.get("Mnemonic"),
                     a.get("LastUpdated")))
    listed = [f"{i} {n!r} ({mn})" for i, n, mn, _ in seen]
    mine = [s for s in seen if s[0] == dataset["id"]]
    if len(mine) != 1:
        raise ValueError(f"the NOMIS search lists the declared dataset "
                         f"{dataset['id']} {len(mine)} time(s); datasets "
                         f"seen: {listed[:40] or 'none'}")
    i, n, mn, lu = mine[0]
    if (n, mn) != (dataset["name"], dataset["mnemonic"]):
        raise ValueError(f"the NOMIS search lists {i} as {n!r} (mnemonic "
                         f"{mn!r}), declared {dataset['name']!r} "
                         f"({dataset['mnemonic']}); the dataset has changed")
    like = [s for s in seen
            if str(s[1] or "").startswith("TS054")
            or TS054_MNEMONIC_RE.fullmatch(str(s[2] or ""))]
    declared = {ds["id"] for ds in DATASETS.values()}
    stray = [s for s in like if s[0] not in declared]
    if stray:
        raise ValueError(
            "the NOMIS search lists a TS054 dataset that is not declared: "
            f"{[f'{a} {b!r} ({c}), LastUpdated {d}' for a, b, c, d in stray]}"
            f"; a new census is declared in DATASETS deliberately, with its "
            f"evidence; not passing over it; datasets seen: {listed[:40]}")
    lines = [f"NOMIS search *TS054*: {len(seen)} dataset(s) listed: "
             f"{', '.join(listed[:12])}",
             f"  {i} {n!r} ({mn}), LastUpdated {lu}: the declared dataset"]
    others = [s for s in like if s[0] != dataset["id"]]
    if others:
        lines.append("  also listed and declared (another census, stored as "
                     "editions only): " + ", ".join(
                         f"{a} ({c})" for a, _, c, _ in others))
    else:
        lines.append("  no other TS054 dataset is listed")
    return lines


def category_codes(codelist_json) -> dict:
    """{live column: code} of the nine categories, each looked up by its
    exact name in the dataset's own codelist (never assumed). ValueError
    when a name is absent or appears twice (listing the names seen), when
    the total is not the codelist's IsTotal code, or when a part's code is
    one of the publisher's aggregates (isDerived, or with children: 1001 to
    1004, 9996 and 9997 in TS054), which is never used as a part."""
    d = _json(codelist_json)
    cls = list((((d.get("structure") or {}).get("codelists") or {})
                .get("codelist")) or [])
    if len(cls) != 1:
        raise ValueError(f"the codelist reply holds {len(cls)} codelists, "
                         "expected one")
    codes = list(cls[0].get("code") or [])
    names = [str(_value(c.get("description"))) for c in codes]
    out = {}
    for col, name in CATEGORIES:
        hits = [c for c, n in zip(codes, names) if n == name]
        if len(hits) != 1:
            raise ValueError(f"{len(hits)} codes named {name!r} in the "
                             f"codelist; names seen: {names}")
        c = hits[0]
        a = _ann(c)
        v = c.get("value")
        if col == TOTAL:
            if str(a.get("IsTotal")) != "True":
                raise ValueError(f"code {v} {name!r} is not the codelist's "
                                 "IsTotal code; the publisher's total is "
                                 "read, never built")
        elif str(a.get("isDerived")) == "True" or "ChildCount" in a:
            raise ValueError(f"code {v} {name!r} is an aggregate of the "
                             "codelist (isDerived, or with children); a part "
                             "must be a leaf category and an aggregate is "
                             "never used as a part")
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f"code {v!r} {name!r} is not an integer code")
        out[col] = v
    return out


def data_url(codes, dataset=None) -> str:
    """The NOMIS data CSV URL: the dataset's latest date, its geography
    TYPE, the nine category codes (VALUES order), measures=20100 and the
    eight selected columns. ValueError unless codes holds the nine."""
    ds = dataset or DATASETS[LIVE_VINTAGE]
    missing = [c for c in VALUES if c not in (codes or {})]
    if missing:
        raise ValueError(f"no category code for {missing}")
    q = (f"date=latest&geography={ds['geography']}&{ds['dimension']}="
         + ",".join(str(codes[c]) for c in VALUES)
         + f"&measures={MEASURES}&select=" + ",".join(select_cols(ds)))
    return f"{NOMIS}/{ds['id']}.data.csv?{q}"


# the code values read from the codelist on 2026-10-10 (total 0, parts 1..8)
LEGACY_FILE["url"] = data_url({c: i for i, c in enumerate(VALUES)})


# ---------------------------------------------------------------------------
# Cells and the CSV
# ---------------------------------------------------------------------------

def int_cell(v, where) -> int:
    """A published integer written as digits (a leading minus allowed;
    checks() rejects a negative count). Anything else raises ValueError
    naming `where`: a blank, any text, a decimal, a thousands separator.
    Never returns a default."""
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ValueError(f"{where}: a blank cell (a blank halts and is never "
                         "read as 0)")
    s = str(v).strip()
    if not re.fullmatch(r"-?[0-9]+", s):
        raise ValueError(f"{where}: {s!r} is not an integer (text halts; it "
                         "is never read as a number or as 0)")
    return int(s)


def read_csv(path, dataset=None) -> dict:
    """The facts of one NOMIS data CSV for a declared dataset (default the
    2021 one): {path, name, census_year, geography_type, record_count, rows
    {E06-E09 code: {column: count}}, names {code: name}, welsh (Welsh
    authorities read and ignored)}. ValueError naming what was seen on: a
    header other than exactly the eight selected columns; a row whose
    GEOGRAPHY_TYPE is not the dataset's (named, with the row); a DATE_NAME
    other than the census year; RECORD_COUNT other than the rows read, or
    RECORD_OFFSET out of sequence (a truncated reply); a category that is
    not one of the nine; a code that is neither E06-E09 nor W06; a repeated
    (code, category); a cell that is not an integer; an authority without
    all nine; a number of English authorities other than EXPECTED_AREAS."""
    ds = dataset or DATASETS[LIVE_VINTAGE]
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"{p.name}: not UTF-8 text ({e})") from None
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError(f"{p.name}: empty")
    want = csv_header(ds)
    if rows[0] != want:
        raise ValueError(f"{p.name}: the header {rows[0]} is not exactly "
                         f"{want}")
    data = rows[1:]
    n = len(data)
    by_name = {name: col for col, name in CATEGORIES}
    out, names, seen, welsh = {}, {}, {}, set()
    for i, r in enumerate(data):
        where = f"{p.name} row {i + 2}"
        if len(r) != len(want):
            raise ValueError(f"{where}: {len(r)} fields, expected "
                             f"{len(want)} ({r[:8]})")
        date_, gname, code, gtype, cat, obs, off, cnt = r
        if gtype != ds["geography_type"]:
            raise ValueError(
                f"{where}: GEOGRAPHY_TYPE {gtype!r}, expected "
                f"{ds['geography_type']!r} on every row (a geography TYPE on "
                "other boundaries gives other districts, such as the "
                "predecessors of the 2023 unitaries)")
        if date_ != ds["census_year"]:
            raise ValueError(f"{where}: DATE_NAME {date_!r}, expected "
                             f"{ds['census_year']!r}")
        if int_cell(cnt, f"{where} RECORD_COUNT") != n:
            raise ValueError(f"{where}: RECORD_COUNT {cnt} but the reply has "
                             f"{n} data rows (a partial reply halts)")
        if int_cell(off, f"{where} RECORD_OFFSET") != i:
            raise ValueError(f"{where}: RECORD_OFFSET {off}, expected {i} "
                             "(rows missing or out of order)")
        if cat not in by_name:
            raise ValueError(f"{where}: category {cat!r} is not one of the "
                             f"nine ({[n_ for _, n_ in CATEGORIES]})")
        col = by_name[cat]
        if (code, col) in seen:
            raise ValueError(f"{where}: {code} {cat!r} appears twice (also "
                             f"row {seen[(code, col)]})")
        seen[(code, col)] = i + 2
        if WELSH_RE.fullmatch(code):
            welsh.add(code)
            continue
        if not DISTRICT_RE.fullmatch(code):
            raise ValueError(f"{where}: {code!r} is neither an English "
                             "(E06-E09) nor a Welsh (W06) authority code")
        if names.setdefault(code, gname) != gname:
            raise ValueError(f"{where}: {code} is named {gname!r} and "
                             f"{names[code]!r}")
        out.setdefault(code, {})[col] = int_cell(obs, f"{where} {code} "
                                                      f"{cat!r}")
    for code, cs in sorted(out.items()):
        miss = [nm for c, nm in CATEGORIES if c not in cs]
        if miss:
            raise ValueError(f"{p.name}: {code} lacks {miss}")
    want_n = globals()["EXPECTED_AREAS"]
    if len(out) != want_n:
        raise ValueError(f"{p.name}: {len(out)} authorities (E06-E09), "
                         f"expected {want_n}")
    return {"path": p, "name": p.name, "census_year": ds["census_year"],
            "geography_type": ds["geography_type"], "record_count": n,
            "rows": out, "names": names, "welsh": len(welsh)}


# ---------------------------------------------------------------------------
# Records and checks
# ---------------------------------------------------------------------------

def _getter(resolve):
    return resolve if callable(resolve) else (lambda c: resolve.get(c))


def build_records(file, resolve) -> list:
    """The census's records from a read CSV, one per authority, sorted by
    lad24cd: {lad24cd, census_year, the nine counts as published (the total
    is the publisher's, never built)}. resolve: {publisher code: lad24cd} or
    a callable. ValueError when a code resolves to nothing or a lad24cd
    repeats."""
    get = _getter(resolve)
    out, seen = [], {}
    for code in sorted(file["rows"]):
        lad = get(code)
        if not lad:
            raise ValueError(f"{code} resolves to no lad24cd")
        if lad in seen:
            raise ValueError(f"duplicate key {lad} ({seen[lad]} and {code}); "
                             "refusing to choose a winner")
        seen[lad] = code
        cs = file["rows"][code]
        out.append({KEY: lad, PERIOD: file["census_year"],
                    **{c: cs[c] for c in VALUES}})
    out.sort(key=lambda x: x[KEY])
    return out


def checks(records) -> list:
    """The checks inside the reply: no count is negative, and the eight
    parts sum to the publisher's total on every authority (the total is
    stored as published, so a disagreement halts; it is never replaced by
    the parts' sum). Returns the lines saying what passed; raises
    ValueError listing every problem."""
    bad = []
    for r in records:
        neg = [c for c in VALUES if r[c] < 0]
        if neg:
            bad.append(f"{r[KEY]}: a negative count in {neg}")
        parts = sum(r[c] for c in PARTS)  # compared only; never stored
        if parts != r[TOTAL]:
            bad.append(f"{r[KEY]}: the eight parts sum to {parts}, the "
                       f"publisher's total is {r[TOTAL]}")
    if bad:
        more = f" and {len(bad) - 20} more" if len(bad) > 20 else ""
        raise ValueError("the reply's own checks failed (the publisher's "
                         "total is stored and never built from the parts, "
                         "so the load halts): " + "; ".join(bad[:20]) + more)
    return [f"the eight parts sum to the publisher's total on all "
            f"{len(records)} authorities", "no negative count"]


def rows_content_sha(records) -> str:
    """sha256 of a census's content: one line per record sorted by lad24cd,
    lad24cd|the nine counts (VALUES order), joined by LF, UTF-8. Nothing
    else is in it, so where or when a reply was read never makes a new
    edition."""
    lines = sorted("|".join([str(r[KEY])] + [str(int(r[c])) for c in VALUES])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------

def resolve_codes(cur, codes) -> tuple:
    """({publisher code: lad24cd}, problems). geography.resolve(cur, '3b',
    codes) (source 3b is declared 'old': a new Barnsley or Sheffield code is
    a problem); then each code's canonical form must be in la_boundaries,
    else 'UNEXPLAINED'. Two codes reaching one lad24cd are a problem."""
    codes = set(codes)
    recode, problems = geography.resolve(cur, RUN_SOURCE, codes)
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
        problems.append(f"codes {two} reach one lad24cd; refusing to choose "
                        "a winner")
    return out, problems


# ---------------------------------------------------------------------------
# Ranks, ledger and planning
# ---------------------------------------------------------------------------

_RANK_RE = re.compile(rf"\bNOMIS LastUpdated ({_TS})")
_LEDGER_RE = re.compile(rf"(.*) \(census_year ([0-9]{{4}}); NOMIS LastUpdated "
                        rf"({_TS})\)")


def release_rank(def_or_source) -> "datetime | None":
    """The NOMIS LastUpdated of a definition (dataset_def's dict), or the
    'NOMIS LastUpdated <time>' an edition's or ledger row's source_file
    carries; None if it names none (a file name, URL or download date is
    never read for a rank)."""
    x = def_or_source
    if isinstance(x, dict):
        return x.get("rank")
    mm = _RANK_RE.search(str(x or ""))
    return _when(mm.group(1), "source_file") if mm else None


def edition_source(d, src, content_sha, note="") -> str:
    """An edition's source_file: the CSV's name, the census year and the
    definition's LastUpdated (the rank), where it was read, the content
    sha, and a note."""
    return (f"{src['file_name']}; census_year {d['census_year']}; "
            f"{rank_text(d['rank'])}; {src['where']}; content sha256 "
            f"{content_sha[:16]}" + (f"; {note}" if note else ""))


def ledger_source(where, year, rank) -> str:
    """A reply's ledger source_file: where it was read (the final data URL,
    or the path of a --file), its census year and LastUpdated, so a later
    run knows them without reading anything else."""
    return f"{where} (census_year {year}; {rank_text(rank)})"


def parse_ledger_source(s) -> "tuple | None":
    """(where, census year, rank) of a ledger source_file, or None."""
    mm = _LEDGER_RE.fullmatch(str(s or ""))
    if not mm:
        return None
    return mm.group(1), mm.group(2), _when(mm.group(3), "ledger")


def content_sha256(path) -> str:
    """SHA-256 of a file's bytes (printed and recorded; for this API output
    it is not a release identity)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tip_ranks(tips, checked) -> dict:
    """{held census: the newest LastUpdated that stated it}: the tip
    edition's, or a later reply the ledger records for it (a reply found
    unchanged stores no edition)."""
    out = {}
    for p, t in tips.items():
        ranks = [t.get("rank")] + [release_rank(s)
                                   for s, _ in checked.get(p, ())]
        ranks = [r for r in ranks if r is not None]
        out[p] = max(ranks) if ranks else None
    return out


def ledger_complete(checked, where, sha, ranks, *, rank=None,
                    allow_older=False, stranded=()) -> "list | None":
    """[census] when the ledger records this reply's (where, content sha)
    pair for the census its ledger source declares, else None (the reply is
    compared). rank: the reply's own LastUpdated (from its definition); when
    given, the ledger row must declare the same one (the rank is in the
    definition, not in the content, so the same content under another
    LastUpdated is compared and planned). A census whose tip comes from a
    newer definition is compared
    again (and then planned as older, which halts) unless allow_older; a
    stranded census (editions, no live rows, live empty) is always
    compared. So a run that failed or was rejected leaves no ledger row and
    the rerun compares again."""
    have, declared = set(), None
    for p, pairs in checked.items():
        for s, h in pairs:
            parsed = parse_ledger_source(s)
            if h == sha and parsed and parsed[0] == where and (
                    rank is None or parsed[2] == rank):
                have.add(str(p))
                declared = parsed
    if declared is None:
        return None
    _, year, rank = declared
    if year not in have or year in {str(x) for x in stranded}:
        return None
    if not allow_older and ranks.get(year) is not None \
            and ranks[year] > rank:
        return None
    return [year]


def plan_year(year, rank, ranks, checked, source, sha, *, recheck,
              allow_older) -> tuple:
    """(new, revised, skipped) for the reply's one census. ranks: {held
    census: tip_ranks}; checked: {census: {(source, sha)}} of the ledger
    (stranded census years left out by the caller). new: not held (unless
    it sorts before the newest held census: skipped as older, unless
    allow_older); revised: held, compared with the tip; skipped {census:
    reason}: held with a tip from a newer definition (older, unless
    allow_older), or already in the ledger (checked, unless --recheck names
    it). --recheck must be the reply's held census (ValueError)."""
    v = str(year)
    if recheck is not None and (recheck != v or v not in ranks):
        raise ValueError(f"--recheck {recheck}: not a held census this reply "
                         f"states ({v})")
    newest_held = max(ranks) if ranks else None
    if v not in ranks:
        if newest_held is not None and v < newest_held and not allow_older:
            return [], [], {v: (f"older: census {v} is before the newest "
                                f"held census {newest_held}")}
        return [v], [], {}
    t = ranks[v]
    if t is not None and t > rank and not allow_older:
        return [], [], {v: (f"older: its tip comes from a definition of "
                            f"{rank_text(t)}, this one is "
                            f"{rank_text(rank)}")}
    if recheck != v and (source, sha) in checked.get(v, ()):
        return [], [], {v: "checked: this reply is already in the ledger"}
    return [], [v], {}


# ---------------------------------------------------------------------------
# Stop conditions
# ---------------------------------------------------------------------------

def _by_key(records) -> dict:
    return {r[KEY]: r for r in records or ()}


def period_problems(new, tip, *, kind, vintage=None, live_empty=False) -> list:
    """The stop conditions of the reply's census (empty = fine); no flag
    releases any of them. revised: an authority of the held edition missing
    from the reply, or one the held edition lacks (the key set of a census
    never changes by a reissue). Any kind (new, or revised: a census held
    as editions only): a census other than LIVE_VINTAGE into an empty live
    table (the table is la_tenure_2021; another census never enters live
    through a load, so with live empty only LIVE_VINTAGE is inserted)."""
    out = []
    if kind == "revised":
        tk, nk = _by_key(tip), _by_key(new)
        gone = sorted(set(tk) - set(nk))
        added = sorted(set(nk) - set(tk))
        if gone:
            out.append(f"{PARTIAL}: {len(gone)} authorit(ies) of the held "
                       "edition missing from the reply (a partial reply "
                       f"never replaces a fuller edition): "
                       f"{', '.join(gone[:8])}")
        if added:
            out.append(f"{PARTIAL}: {len(added)} authorit(ies) not in the "
                       "held edition (a reissue never changes a census's "
                       f"key set): {', '.join(added[:8])}")
    if live_empty and vintage is not None and str(vintage) != LIVE_VINTAGE:
        # every kind: a vintage held as editions only (planned as
        # revised) must not be inserted into an empty live table either
        out.append(f"census {vintage} into an empty live table: the live "
                   f"table holds census {LIVE_VINTAGE} only "
                   "(la_tenure_2021), and another census never enters live "
                   "through a load (adopting one is a deliberate decision); "
                   f"load {LIVE_VINTAGE} first, then {vintage} is stored as "
                   "editions only")
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
    name="s3b",
    live_table="la_tenure_2021",
    editions_table="la_tenure_2021_editions",
    key_cols=(KEY,),
    period_col=PERIOD,
    value_cols=VALUE_TYPES,
    # the nine counts, never the generated totals; source and loaded_at are
    # written on the rows a refresh changes, never compared
    refresh_cols=VALUES + ("source", "loaded_at"),
    refresh_from=(("source", "release_label"), ("loaded_at", "loaded_at")),
    key_types=((KEY, "varchar(9) NOT NULL"), (PERIOD, "integer NOT NULL")),
    table_constraints=(f"CHECK ({PERIOD} BETWEEN 1000 AND 9999)",),
    fk_la_boundaries=True,
    live_single_period=True,            # one census in live
    refresh_key_changes=False,          # the key set never changes
)


def _no_label(fetched_on) -> str:
    raise ValueError("an S3b release label comes from the definition; store "
                     "through apply_year(...)")


def check_records(records, period) -> None:
    """ValueError unless the census's records are whole: each of this
    census, one per lad24cd, every count present."""
    seen = set()
    for r in records:
        if r.get(PERIOD) != period:
            raise ValueError(f"{period}: a record of census "
                             f"{r.get(PERIOD)!r}")
        if r[KEY] in seen:
            raise ValueError(f"{period}: {r[KEY]} appears twice")
        if any(r.get(c) is None for c in VALUES):
            raise ValueError(f"{period}: {r[KEY]} lacks a value")
        seen.add(r[KEY])


PROFILE = pe.Profile(
    spec=SPEC, value_cols=VALUES, run_agent=RUN_AGENT, run_source=RUN_SOURCE,
    heading="S3b ONS Census 2021 TS054 Tenure, NOMIS (la_tenure_2021)",
    default_source_file="ONS Census 2021 TS054 Tenure, NOMIS NM_2072_1",
    expected_areas=EXPECTED_AREAS, release_label=_no_label,
    content_sha256=rows_content_sha, check_records=check_records,
    file_checks=True, savepoint="s3b_period",
    example_label="(the nine tenure counts)")


def profile() -> pe.Profile:
    """PROFILE bound to the current SPEC and EXPECTED_AREAS (looked up when
    called, so tests can swap them)."""
    g = globals()
    return dataclasses.replace(PROFILE, spec=g["SPEC"],
                               expected_areas=g["EXPECTED_AREAS"])


def _late(name: str):
    """A call through this module's attribute `name`, looked up when
    called (so mock.patch.object(s3b_tenure_editions, name, ...) reaches
    the engine)."""
    return lambda *a, **kw: globals()[name](*a, **kw)


def create_all(cur) -> None:
    """ddl: the editions table, its append-only triggers and the file-check
    ledger. Idempotent; no value is written; the live table is not
    touched."""
    prof = profile()
    core.create_schema(cur, prof.spec)
    pe.create_file_checks(cur, prof)


def insert_live(cur, profile, period: str, records: list) -> None:
    """A census's live rows: the key, the census year, the nine counts and
    source (each record's live_source); loaded_at takes its default and the
    generated totals compute themselves."""
    from psycopg2.extras import execute_values
    spec = profile.spec
    cols = (KEY, spec.period_col) + VALUES + ("source",)
    execute_values(
        cur, f"INSERT INTO public.{spec.live_table} ({', '.join(cols)}) "
        "VALUES %s",
        [(r[KEY], int(period)) + tuple(r[c] for c in VALUES)
         + (r["live_source"],) for r in records], page_size=1000)


@dataclass(frozen=True)
class PeriodInfo:
    """What a census's edition records: source_file, the release label and
    the reply read ((ledger source, content sha),)."""
    source_file: str
    label: str
    files: tuple


def _tip_label(cur, spec, period) -> str:
    tip = core.chain_tip(cur, spec, period)
    cur.execute(f"SELECT DISTINCT release_label FROM "
                f"public.{spec.editions_table} WHERE {spec.period_col} = %s "
                "AND edition = %s", (period, tip))
    return cur.fetchone()[0]


def apply_year(cur, profile, period, records, *, fetched_on, info) -> str:
    """pe.apply_period for the census, then its ledger row, inside the
    caller's per-period savepoint (s3b_period): edition, live rows (the
    first census, or a stranded one) and ledger row commit or roll back
    together. Live source is the edition's release label (a stranded
    census's live rows take the tip edition's)."""
    kind = pe.classify_period(cur, profile, period, records)
    live_source = (_tip_label(cur, profile.spec, period)
                   if kind == LIVE_MISSING else info.label)
    records = [dict(r, live_source=live_source) for r in records]
    prof = dataclasses.replace(profile, release_label=lambda d: info.label)
    kind = pe.apply_period(cur, prof, period, records, fetched_on=fetched_on,
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


def fetch_json(url, session=None) -> dict:
    """A NOMIS JSON reply (a read-only GET), not saved (the search and the
    codelist)."""
    try:
        return json.loads(_get(_session(session), url).content)
    except ValueError as e:
        halt(f"GET {url}: not JSON ({e})")


def _check_body(dest: Path, body: bytes) -> None:
    """Raise unless body is what dest's suffix promises: a NOMIS data CSV
    (its first line the quoted DATE_NAME header) or a JSON document."""
    suffix = dest.suffix.lower()
    if suffix == ".csv":
        if not body.lstrip(b"\xef\xbb\xbf").startswith(b'"DATE_NAME"'):
            raise ValueError(f"not a NOMIS data CSV (starts {body[:80]!r})")
    elif suffix == ".json":
        json.loads(body)
    else:
        raise ValueError(f"{dest.name}: neither .csv nor .json")


def fetch_api(url, dest, session=None) -> tuple:
    """Download url (redirects followed) to the file dest (a dated name
    under RAW_DIR, such as NM_2072_1_TYPE424_2026-10-11.csv) and return
    (path to read, final URL). Checked before it is kept: a .csv must be a
    NOMIS data CSV, a .json must parse. A same-named file with the same
    content is kept as it is; one with different content is never replaced:
    the download is saved beside it as <stem>-<sha8><suffix> and that is the
    one read. An existing name is never a reason to skip the download."""
    dest = Path(dest)
    r = _get(_session(session), url)
    final = str(getattr(r, "url", None) or url)
    if final != url:
        print(f"redirected: {url} -> {final}")
    body = r.content
    try:
        _check_body(dest, body)
    except ValueError as e:
        halt(f"{final}: {e}; nothing written")
    dest.parent.mkdir(parents=True, exist_ok=True)
    new_sha = hashlib.sha256(body).hexdigest()
    path = dest
    if dest.exists():
        old_sha = content_sha256(dest)
        if old_sha == new_sha:
            print(f"{dest.name} already held with the same content (sha256 "
                  f"{new_sha[:16]}); kept as it is")
            return path, final
        path = dest.with_name(f"{dest.stem}-{new_sha[:8]}{dest.suffix}")
        if path.exists() and content_sha256(path) == new_sha:
            print(f"NOTE: {dest.name} differs from this download; "
                  f"{path.name} already holds it (sha256 {new_sha[:16]})")
            return path, final
        print(f"NOTE: {dest.name} already exists with different content "
              f"(sha256 {old_sha[:16]}, new {new_sha[:16]}); kept it "
              f"untouched and saved the new download as {path.name}")
    else:
        print(f"downloaded {dest.name}: {len(body):,} bytes, sha256 "
              f"{new_sha[:16]}")
    tmp = path.with_name(path.name + ".download.tmp")
    tmp.write_bytes(body)
    tmp.replace(path)
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
    """The census's rows as records (lad24cd, the census year as a string,
    the nine counts): the live table's (edition None) or the given
    edition's."""
    names = (KEY,) + VALUES
    if edition is None:
        cur.execute(f"SELECT {', '.join(names)} FROM public.{spec.live_table} "
                    f"WHERE {spec.period_col} = %s", (int(period),))
    else:
        cur.execute(f"SELECT {', '.join(names)} FROM "
                    f"public.{spec.editions_table} WHERE {spec.period_col} = "
                    "%s AND edition = %s", (int(period), edition))
    out = [dict(zip(names, r)) for r in cur.fetchall()]
    for r in out:
        r[spec.period_col] = str(period)
    return out


def tip_info(cur, spec) -> dict:
    """{census (string): {edition, source_file, rank}} of every census with
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


def _live_rows(cur, spec) -> int:
    cur.execute(f"SELECT COUNT(*) FROM public.{spec.live_table}")
    return cur.fetchone()[0]


def legacy_tips(cur, spec) -> dict:
    """Before the editions table exists: {LIVE_VINTAGE: {rank}} when live
    holds rows (the held definition's LastUpdated, LEGACY_RANK), else {}."""
    if not _live_rows(cur, spec):
        return {}
    return {LIVE_VINTAGE: {"edition": None, "source_file": None,
                           "rank": LEGACY_RANK}}


def ledger_checks(cur, prof) -> dict:
    """{census: {(source_file, file_sha256)}} of the ledger."""
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
    '3b'). Called only on committed load, migrate-legacy and restore-edition
    runs, partial runs included."""
    pe.log_run(cur, profile(), rows_written, notes, started_at)


def _readiness(cur, prof) -> dict:
    spec = prof.spec
    lt = pe.file_checks_table(prof)
    return {spec.editions_table: table_exists(cur, spec.editions_table),
            lt: table_exists(cur, lt)}


def cmd_ddl(args) -> int:
    """ddl [--commit | --simulate]: the editions table and its triggers and
    the ledger. The live table is not touched."""
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            for t, ok in _readiness(cur, prof).items():
                print(f"{t}: {'exists' if ok else 'does not exist'}")
            print(f"{spec.live_table}: not touched by ddl")
            if not writing:
                conn.rollback()
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return 0
            create_all(cur)
        pe.finish(conn, args, f"ddl: {spec.editions_table} and triggers "
                              f"{spec.trigger}, {spec.truncate_trigger}, "
                              f"{pe.file_checks_table(prof)} present")
        return 0
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def status_lines(cur, prof) -> list:
    """Per census: the tip edition, its LastUpdated and the ledger's latest
    reply."""
    s = prof.spec
    lt = pe.file_checks_table(prof)
    lines = []
    for p, t in sorted(tip_info(cur, s).items()):
        line = f"  {p}: tip edition {t['edition']} ({rank_text(t['rank'])})"
        cur.execute(f"SELECT source_file, outcome FROM public.{lt} WHERE "
                    f"{s.period_col} = %s ORDER BY id DESC LIMIT 1", (int(p),))
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
            missing = [t for t, ok in _readiness(cur, prof).items() if not ok]
            if missing:
                print(f"status: {' and '.join(missing)} "
                      f"{'does' if len(missing) == 1 else 'do'} not exist "
                      "yet; run `ddl --commit`, then `migrate-legacy --file "
                      "CSV --def JSON --commit` (S3b has no sync-new)")
                return 1
            st = pe.status(cur, prof)
            lines = status_lines(cur, prof)
    finally:
        conn.rollback()
        conn.close()
    print(pe.format_status(prof, st, lines))
    return 0 if st["ok"] else 1


def _tables_exist(cur) -> bool:
    """True when the editions table and ledger exist; halts (S3b's own
    wording: there is no sync-new) when one does not."""
    for t, ok in _readiness(cur, profile()).items():
        if not ok:
            halt(f"{t} does not exist yet; run `ddl --commit`, then "
                 "`migrate-legacy --file CSV --def JSON --commit`")
    return True


def cmd_refresh_latest(args) -> int:
    """refresh-latest [--accept-drift YEAR] [--commit | --simulate]: the
    engine's refresh of the live census only (its latest edition copied into
    live on the rows that differ, loaded_at and source included, under the
    before/after hash guard); any other census named halts."""
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
    if not YEAR_RE.fullmatch(str(text or "")):
        raise argparse.ArgumentTypeError(f"not a census year YYYY: {text!r}")
    return str(text)


def _read_def(path) -> dict:
    p = Path(path)
    if not p.is_file():
        halt(f"--def {path}: no such file")
    try:
        return dataset_def(p.read_text(encoding="utf-8"))
    except ValueError as e:
        halt(f"{p.name}: definition identity check failed, nothing stored: "
             f"{e}")


def _read(path, dataset) -> dict:
    try:
        return read_csv(path, dataset)
    except ValueError as e:      # the message names the file already
        halt(f"{e}; nothing stored")


def _report(d, f) -> None:
    print(f"definition: {d['id']} {d['name']!r} ({d['mnemonic']}), census "
          f"{d['census_year']}; FirstReleased "
          f"{d['first_released'].strftime(_TS_FMT)}; "
          f"{rank_text(d['rank'])} (the rank)")
    print(f"{f['name']}: {f['record_count']} rows (RECORD_COUNT agrees), "
          f"GEOGRAPHY_TYPE {f['geography_type']!r} on every row; "
          f"{len(f['rows'])} English authorities x 9 stored, {f['welsh']} "
          "Welsh authorities read and ignored")


def _source(args) -> dict:
    """The reply the run reads: {csv, where, how, file_name, def}. --file
    and --def read local files (no network; the definition gives the
    dataset); otherwise the NOMIS search, definition, codelist and data CSV
    are read and the definition and CSV saved to RAW_DIR with the date (also
    in a preview)."""
    if args.file or args.def_:
        if not (args.file and args.def_):
            halt("--file and --def go together: the CSV and the definition "
                 "it was read with (identity is read from the definition)")
        path = Path(args.file)
        if not path.is_file():
            halt(f"--file {args.file}: no such file")
        print(f"files given: {path} and {args.def_}; nothing downloaded; "
              "identity is read from the definition and the CSV")
        d = _read_def(args.def_)
        return {"csv": path, "where": _file_source(path), "how": "local file",
                "file_name": path.name, "def": d}
    ds = DATASETS[LIVE_VINTAGE]
    try:
        lines = discover(fetch_json(search_url()), ds)
    except ValueError as e:
        halt(f"{search_url()}: {e}")
    for line in lines:
        print(line)
    today = date.today().isoformat()
    print(f"downloading to {RAW_DIR} (also in a preview; the database is "
          "not written)")
    dpath, _ = fetch_api(def_url(ds), RAW_DIR / f"{ds['id']}_def_{today}.json")
    d = _read_def(dpath)
    if d["id"] != ds["id"]:
        halt(f"{def_url(ds)}: the definition is {d['id']}, not {ds['id']}")
    try:
        codes = category_codes(fetch_json(codelist_url(ds)))
    except ValueError as e:
        halt(f"{codelist_url(ds)}: {e}")
    print("category codes from the codelist: " + ", ".join(
        f"{c}={codes[c]}" for c in VALUES))
    url = data_url(codes, ds)
    path, final = fetch_api(url, RAW_DIR / f"{ds['id']}_{ds['geography']}_"
                                           f"{today}.csv")
    print(f"  final URL: {final}")
    return {"csv": path, "where": final, "how": "NOMIS API",
            "file_name": path.name, "def": d}


def _label(d, src, sha, bsha, how, *, notes=()) -> str:
    return (f"ONS Census {d['census_year']} {d['name']}, NOMIS {d['id']} "
            f"({d['mnemonic']}), {d['dataset']['geography_type']}; "
            f"{rank_text(d['rank'])}; {src['file_name']}; {src['where']}; "
            f"content sha256 {sha[:16]}, bytes sha256 {bsha[:16]}; {how}; "
            "published_date is the LastUpdated date"
            + "".join(f"; {n}" for n in notes if n))


def _build(cur, file) -> list:
    """The census's records, codes resolved and the reply's own checks run;
    halts on a geography or check problem."""
    try:
        rmap, problems = resolve_codes(cur, set(file["rows"]))
    except (ValueError, KeyError) as e:
        halt(f"geography: {e}; nothing stored")
    if problems:
        halt("geography check failed, nothing stored: " + "; ".join(problems))
    recoded = sorted((c, r) for c, r in rmap.items() if r != c)
    if recoded:
        print("codes resolved (geography.resolve, la_code_lookup recode): "
              + ", ".join(f"{c} -> {r}" for c, r in recoded))
    try:
        recs = build_records(file, rmap)
        lines = checks(recs)
    except ValueError as e:
        halt(f"{e}; nothing stored")
    print("checks passed: " + "; ".join(lines))
    return recs


def _preview_live(cur, spec, period, recs) -> None:
    """Before the editions table exists: the reply against the live rows
    (the nine counts; NULL never equals a number)."""
    if period != LIVE_VINTAGE:
        print(f"  {period}: would be held as editions only (live holds "
              f"{LIVE_VINTAGE}; compared with nothing)")
        return
    held = records(cur, spec, period)
    if not held:
        print(f"  {period}: new, {len(recs)} areas (live is empty)")
        return
    hk, nk = _by_key(held), _by_key(recs)
    diff = [k for k in sorted(set(hk) | set(nk))
            if k not in hk or k not in nk
            or any(hk[k][c] != nk[k][c] for c in VALUES)]
    ex = [f"{k}: {tuple(hk[k][c] for c in VALUES) if k in hk else 'absent'}"
          f" -> {tuple(nk[k][c] for c in VALUES) if k in nk else 'absent'}"
          for k in diff[:3]]
    print(f"  {period}: {'revised' if diff else 'unchanged'}, {len(diff)} "
          "areas changed (compared with live)"
          + ("; " + "; ".join(ex) if ex else ""))


def cmd_load(args) -> int:
    prof = profile()
    spec = prof.spec
    writing = args.commit or args.simulate
    conn = _conn(writing)
    try:
        with conn.cursor() as cur:
            ready = _readiness(cur, prof)
            has_ed = all(ready.values())
            missing = [t for t, ok in ready.items() if not ok]
            if writing and not has_ed:
                halt(f"missing {', '.join(missing)}; run `ddl --commit` and "
                     "`migrate-legacy --file CSV --def JSON --commit` first")
            live_empty = _live_rows(cur, spec) == 0
            if has_ed:
                _, new_live, errors = core.latest_map(cur, spec)
                if errors:
                    halt("invalid edition chain: " + "; ".join(
                        f"{pe._p(p)}: {msg}" for p, msg in errors.items()))
                if new_live:
                    halt(f"{spec.live_table}: live census years with no "
                         f"editions {[pe._p(x) for x in new_live]}; run "
                         "migrate-legacy first")
                tips = tip_info(cur, spec)
                checked = ledger_checks(cur, prof)
                stranded = (pe.live_missing_periods(cur, prof) if live_empty
                            else [])
            else:
                print(f"NOTE: {', '.join(missing)} not present: no editions "
                      "table yet; this preview compares the file with the "
                      "LIVE table")
                tips, checked, stranded = legacy_tips(cur, spec), {}, []
            ranks = tip_ranks(tips, checked)
            held = sorted(ranks)
            print("held census years: " + (", ".join(held) or "none"))
            src = _source(args)
            d = src["def"]
            f = _read(src["csv"], d["dataset"])
            _report(d, f)
            recs = _build(cur, f)
            period = f["census_year"]
            sha = rows_content_sha(recs)
            bsha = content_sha256(src["csv"])
            print(f"{src['file_name']}: content sha256 {sha[:16]}"
                  + ("; the held reply's content"
                     if sha == LEGACY_FILE["content_sha256"] else "")
                  + f"; bytes sha256 {bsha[:16]} (API output: not a release "
                  "identity)")
            if has_ed and args.recheck is None:
                done = ledger_complete(checked, src["where"], sha, ranks,
                                       rank=d["rank"],
                                       allow_older=args.allow_older_file,
                                       stranded=stranded)
                if done:
                    print(f"the reply's (source, content sha256) is already "
                          f"in the ledger for {', '.join(done)}; nothing "
                          "compared (--recheck YEAR compares it again)")
                    print("nothing to do")
                    return _end(conn, args, writing, rc=0)
            lsrc = ledger_source(src["where"], period, d["rank"])
            chk = {p: s for p, s in checked.items() if p not in stranded}
            try:
                new, revised, skipped = plan_year(
                    period, d["rank"], ranks, chk, lsrc, sha,
                    recheck=args.recheck, allow_older=args.allow_older_file)
            except ValueError as e:
                halt(str(e))
            rc = _run(args, conn, cur, prof, has_ed, src, d, recs, new,
                      revised, skipped, tips, ranks, held, sha, bsha, lsrc,
                      live_empty)
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


def _reissue_problem(p, rank, t) -> str:
    if t is not None and rank == t:
        why = (f"the definition has {rank_text(rank)}, the same as the held "
               "tip's, but the content differs: two sources claim the same "
               "release")
    elif t is not None and rank > t:
        why = (f"a reissue: the definition has {rank_text(rank)}, later than "
               f"the held tip's {rank_text(t)}, and the content differs")
    else:
        why = (f"the definition has {rank_text(rank)} (held tip "
               f"{rank_text(t)}) and the content differs")
    return (f"{why}. Census counts are not expected to change: read the "
            f"dataset's notes on NOMIS, then --accept-reissue {p}")


def _run(args, conn, cur, prof, has_ed, src, d, recs, new, revised, skipped,
         tips, ranks, held, sha, bsha, lsrc, live_empty) -> int:
    started = datetime.now(timezone.utc)
    spec = prof.spec
    rank = d["rank"]
    older_note = {}
    for p in revised + new:
        t = ranks.get(p)
        if t is not None and t > rank:
            older_note[p] = (f"--allow-older-file given; {p}'s tip comes from "
                             f"a definition of {rank_text(t)}, this one is "
                             f"{rank_text(rank)}")
        elif p in new and held and p < max(held):
            older_note[p] = (f"--allow-older-file given; census {p} is "
                             f"before the newest held census {max(held)}")
        if p in older_note:
            print(f"NOTE: {older_note[p]}")
    print(f"planned: new {new or 'none'}; compare {revised or 'none'}; "
          "skipped " + (", ".join(f"{p} ({r})" for p, r in sorted(
              skipped.items())) or "none"))
    periods = sorted(new + revised)
    reissue = set(args.accept_reissue or ())
    stray = sorted(reissue - set(periods))
    if stray:
        halt(f"--accept-reissue {stray}: not census years this run compares "
             f"or stores ({periods or 'none'}); nothing stored")
    if not periods:
        if skipped and all(r.startswith("older") for r in skipped.values()):
            halt(f"older definition: census {d['census_year']}, "
                 f"{rank_text(rank)}, older than what is held for "
                 f"{', '.join(sorted(skipped))}; storing it would record an "
                 "older release. If this is deliberate, re-run with "
                 "--allow-older-file")
        print("nothing to do")
        return 0
    problems, infos, reissued = {}, {}, {}
    for p in periods:
        if p in revised:
            kind = "revised"
            tip = _tip_records(cur, spec, p, tips, has_ed)
            bad = period_problems(recs, tip, kind=kind, vintage=p,
                                  live_empty=live_empty)
            if _differs(recs, tip):
                if p in reissue:
                    reissued[p] = (f"--accept-reissue {p}: the definition has "
                                   f"{rank_text(rank)} (held tip "
                                   f"{rank_text(ranks.get(p))}) and the "
                                   "content differs (a publisher reissue)")
                    print(f"  {p}: ACCEPTED REISSUE: {reissued[p]}")
                else:
                    bad.append(_reissue_problem(p, rank, ranks.get(p)))
        else:
            kind = "new"
            bad = period_problems(recs, None, kind=kind, vintage=p,
                                  live_empty=live_empty)
        if bad:
            problems[p] = bad
            continue
        notes = (older_note.get(p, ""), reissued.get(p, ""))
        how = (("new census" if kind == "new" else "restated census")
               + f" ({src['how']})")
        infos[p] = PeriodInfo(
            edition_source(d, src, sha, "; ".join(n for n in notes if n)),
            _label(d, src, sha, bsha, how, notes=notes), ((lsrc, sha),))
    for p, msgs in sorted(problems.items()):
        for msg in msgs:
            print(f"  {p}: REJECTED, not stored: STOP CONDITION: {msg}")
    ok = [p for p in periods if p not in problems]
    rc = 0
    stats = {"periods": [], "kinds": {}, "stored_rows": 0, "live_rows": 0}
    if ok and has_ed:
        def apply(c, prof_, per, rs, *, fetched_on, source_file=None):
            return _late("apply_year")(c, prof_, per, rs,
                                       fetched_on=fetched_on,
                                       info=infos[per])
        rc = pe.load_periods(cur, prof, ok, lambda per: recs, rank.date(),
                             args.commit, simulate=args.simulate,
                             against="editions", stats=stats, apply=apply)
    elif ok:
        for p in ok:
            _preview_live(cur, spec, p, recs)
        print("  (no editions table yet: preview against live only)")
    if problems:
        print(f"REJECTED {len(problems)} census year(s), nothing stored for "
              f"them: {', '.join(sorted(problems))}; exit 1")
        rc = 1
    if args.commit:
        stored = live = 0  # rows written by this run; not a source value
        for p in stats["periods"]:
            k = stats["kinds"][p]
            if k in ("new", "revised", HELD_NOT_LIVE):
                stored += len(recs)
            if k in ("new", LIVE_MISSING):
                live += len(recs)
        left = [p for p in ok if p not in stats["periods"]]
        failed, not_tried = (left[:1], left[1:]) if left else ([], [])
        partial = ""
        if rc:
            partial = (f"PARTIAL RUN (exit 1): rejected "
                       f"{sorted(problems) or 'none'}"
                       + "".join(f" ({p}: {'; '.join(problems[p])[:300]})"
                                 for p in sorted(problems))
                       + f"; failed {failed or 'none'}; not attempted "
                       f"{not_tried or 'none'}; a census that failed or was "
                       "rejected stored nothing and is not in the counts "
                       "below; ")
        notes = (partial + f"NOMIS {d['id']} {src['where']} content sha256 "
                 f"{sha[:16]}, census {d['census_year']}, {rank_text(rank)} "
                 f"({src['how']}): "
                 + ("; ".join(f"{p} {k}" for p, k in stats["kinds"].items())
                    or "nothing stored")
                 + (f"; skipped {sorted(skipped)}" if skipped else "")
                 + "".join(f"; {older_note[p]}" for p in sorted(older_note)
                           if p in stats["periods"])
                 + "".join(f"; {reissued[p]}" for p in sorted(reissued)
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

SURVEY_COLS = (KEY,) + VALUES + (PERIOD, "source")


def live_state(cur, spec=None) -> tuple:
    """(rows, survey hash, {loaded_at (UTC text): rows}, census years) of
    the live table. The survey hash is md5 of lad24cd|the nine counts|
    census_year|source (NULL as '~'), rows ordered by census_year, lad24cd,
    joined by LF (the generated totals and loaded_at left out)."""
    spec = spec or globals()["SPEC"]
    expr = "||'|'||".join(f"coalesce({c}::text,'~')" for c in SURVEY_COLS)
    cur.execute(f"SELECT COUNT(*), md5(string_agg({expr}, E'\\n' ORDER BY "
                f"{PERIOD}, {KEY})) FROM public.{spec.live_table}")
    n, h = cur.fetchone()
    cur.execute(f"SELECT to_char(loaded_at AT TIME ZONE 'UTC', "
                f"'YYYY-MM-DD HH24:MI:SS.US') || '+00', COUNT(*) FROM "
                f"public.{spec.live_table} GROUP BY 1 ORDER BY 1")
    loaded = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT {PERIOD} FROM public.{spec.live_table} "
                "ORDER BY 1")
    years = [str(r[0]) for r in cur.fetchall()]
    return n, h, loaded, years


def _proof(held, built) -> tuple:
    """(counts compared, differences) of the held rows against the rebuilt
    rows on the nine counts."""
    hk, bk = _by_key(held), _by_key(built)
    diffs = [("only held", k) for k in sorted(set(hk) - set(bk))]
    diffs += [("only rebuilt", k) for k in sorted(set(bk) - set(hk))]
    cells = 0  # counts compared; not a source value
    for k in sorted(set(hk) & set(bk)):
        for c in VALUES:
            cells += 1
            if hk[k][c] != bk[k][c]:
                diffs.append((k, c, hk[k][c], bk[k][c]))
    return cells, diffs


def migrate_legacy(cur, csv_path, def_path, *, write, legacy=None,
                   loaded=None, files=None) -> dict:
    """One-off. Preconditions: on write the editions table and ledger exist
    (ddl first) and are empty (in a preview they may be absent; if present
    they must be empty); live as surveyed (legacy: (rows, survey hash),
    default LEGACY_LIVE; loaded: {loaded_at: rows}, default LEGACY_LOADED;
    census year 2021 only). Proof, any failure halts: the definition's
    identity and its LastUpdated equal to LEGACY_RANK; the CSV's identity
    and the reply's own checks (the parts sum to the publisher's total); its
    content sha as files['content_sha256'] (default LEGACY_FILE; the bytes
    are API output); and this parser on the CSV reproduces every live count
    (296 x 9). Edition 1 'as loaded' from live (published_date the latest
    load date, 2026-08-20, AS_LOADED_LATEST_LABEL; source_file 'as loaded:
    <name>; census_year 2021; NOMIS LastUpdated <time>'), and a ledger row
    for the reply (outcome 'unchanged', edition 1, files['url'], the content
    sha) so the next API load finds it checked. Live untouched (both source
    variants and both loaded_at values kept). Nothing commits here; the
    caller rolls everything back on a halt."""
    g = globals()
    legacy = tuple(legacy or g["LEGACY_LIVE"])
    loaded_want = dict(loaded or g["LEGACY_LOADED"])
    files = files or g["LEGACY_FILE"]
    prof = profile()
    spec = prof.spec
    ready = _readiness(cur, prof)
    if write and not all(ready.values()):
        halt(f"missing {[t for t, ok in ready.items() if not ok]}; run "
             "`ddl --commit` first")
    for t in (spec.editions_table, pe.file_checks_table(prof)):
        if ready[t]:
            cur.execute(f"SELECT COUNT(*) FROM public.{t}")
            n = cur.fetchone()[0]
            if n:
                halt(f"{t} already holds {n} rows; migrate-legacy runs once, "
                     "on empty tables")
    n, h, loaded_got, years = live_state(cur, spec)
    if (n, h) != legacy or years != [LIVE_VINTAGE]:
        halt(f"{spec.live_table} is not as surveyed: {n} rows, hash {h}, "
             f"census years {years}; expected {legacy[0]} rows, hash "
             f"{legacy[1]}, census {LIVE_VINTAGE}; nothing stored")
    if loaded_got != loaded_want:
        halt(f"{spec.live_table} loaded_at is not as surveyed: {loaded_got}; "
             f"expected {loaded_want}; nothing stored")
    d = _read_def(def_path)
    if d["census_year"] != LIVE_VINTAGE or d["rank"] != LEGACY_RANK:
        halt(f"{Path(def_path).name}: census {d['census_year']}, "
             f"{rank_text(d['rank'])}; expected census {LIVE_VINTAGE}, "
             f"{rank_text(LEGACY_RANK)} (the held definition); nothing "
             "stored")
    path = Path(csv_path)
    f = _read(path, d["dataset"])
    _report(d, f)
    period = f["census_year"]
    held = records(cur, spec, period)
    built = _build(cur, f)
    sha = rows_content_sha(built)
    bsha = content_sha256(path)
    if sha != files["content_sha256"]:
        halt(f"{path.name}: content sha256 {sha[:16]}, expected "
             f"{files['content_sha256'][:16]} (the held reply); nothing "
             "stored")
    cells, diffs = _proof(held, built)
    if diffs:
        halt(f"proof failed: {len(diffs)} differences between the held rows "
             f"and the held reply read again, e.g. {diffs[:5]}; nothing "
             "stored")
    cur.execute(f"SELECT MAX((loaded_at AT TIME ZONE 'UTC')::date) FROM "
                f"public.{spec.live_table}")
    latest = cur.fetchone()[0]
    plan = {"period": period, "rows": len(held), "cells": cells,
            "records": held, "sha": sha, "bytes_sha": bsha, "loaded": latest,
            "content_sha": rows_content_sha(held),
            "source_file": (f"as loaded: {files['name']}; census_year "
                            f"{period}; {rank_text(d['rank'])}"),
            "ledger": ledger_source(files["url"], period, d["rank"])}
    print(f"proof: the held reply read again reproduces every held count: "
          f"{len(held):,} authorities matched by lad24cd, {cells:,} counts "
          f"(the nine categories), 0 differences; the parts sum to the "
          f"publisher's total on all {len(built)}")
    print(f"  {period}: edition 1 as loaded, {len(held):,} rows, published "
          f"{latest} (the latest load date; live loaded_at "
          f"{sorted(loaded_got.items())}); source_file "
          f"{plan['source_file']!r}; content sha256 {plan['content_sha']}; "
          f"bytes sha256 {bsha[:16]}; ledger {plan['ledger']}")
    if not write:
        return plan
    cur.execute(f"SAVEPOINT {prof.savepoint}")
    ed = core.insert_edition(
        cur, spec, held, period, release_label=core.AS_LOADED_LATEST_LABEL,
        published_date=latest, source_file=plan["source_file"],
        source_sha256=plan["content_sha"], supersedes=None, strict=True)
    if ed != 1 or core.rows_differing(cur, spec, period, ed):
        halt(f"{spec.editions_table} {period}: edition 1 does not equal "
             "live; rolled back")
    pe.record_file_check(cur, prof, period, plan["ledger"], sha,
                         "unchanged", 1)
    cur.execute(f"RELEASE SAVEPOINT {prof.savepoint}")
    return plan


def cmd_migrate_legacy(args) -> int:
    if not (args.file and args.def_):
        halt("migrate-legacy needs --file CSV and --def JSON (the held NOMIS "
             "reply and its definition)")
    writing = args.commit or args.simulate
    conn = _conn(writing)
    started = datetime.now(timezone.utc)
    try:
        with conn.cursor() as cur:
            plan = migrate_legacy(cur, args.file, args.def_, write=writing)
            if args.commit:
                log_run(cur, plan["rows"], f"migrate-legacy: edition 1 as "
                        f"loaded for census {plan['period']} ({plan['rows']} "
                        f"rows) from the live table; proof {plan['cells']} "
                        "counts equal the held NOMIS reply read again "
                        f"(content sha256 {plan['sha'][:16]}); ledger row "
                        "for the reply. Live untouched.", started)
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
                f" WHERE {spec.period_col} = %s AND edition = %s",
                (int(p), edition))
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
        description="S3b ONS Census 2021 TS054 Tenure (NOMIS NM_2072_1): "
        "editions loader on the period-editions engine, one census in live. "
        "There is no sync-new; migrate-legacy records the held census "
        "once.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ddl", help="create the editions table, its triggers "
                       "and the file-check ledger (preview by default)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_ddl)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    p = sub.add_parser("load", help="read the NOMIS dataset, compare and "
                       "store its census (preview by default)")
    p.add_argument("--file", metavar="CSV",
                   help="a local NOMIS data CSV (with --def; nothing "
                   "downloaded)")
    p.add_argument("--def", dest="def_", metavar="JSON",
                   help="the dataset definition JSON the CSV was read with")
    p.add_argument("--recheck", type=_year_arg, metavar="YEAR",
                   help="compare this held census again although the reply "
                   "is in the ledger")
    p.add_argument("--allow-older-file", action="store_true",
                   help="compare and store a census whose tip comes from a "
                   "newer definition, or a census before the newest held "
                   "(logged)")
    p.add_argument("--accept-reissue", action="append", type=_year_arg,
                   metavar="YEAR",
                   help="store a held census whose content differs (a "
                   "publisher reissue; repeatable; logged)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_load)
    p = sub.add_parser("refresh-latest", help="copy the live census's latest "
                       "edition into the live table, loaded_at included "
                       "(preview by default)")
    p.add_argument("--accept-drift", action="append", metavar="YEAR",
                   help="overwrite the live census although its live rows "
                   "equal no stored edition (repeatable; any other census "
                   "halts)")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_refresh_latest)
    p = sub.add_parser("migrate-legacy", help="one-off: edition 1 as loaded "
                       "for the held census, with the proof (preview by "
                       "default)")
    p.add_argument("--file", metavar="CSV", help="the held NOMIS data CSV")
    p.add_argument("--def", dest="def_", metavar="JSON",
                   help="the held dataset definition JSON")
    pe.mode_parser(p)
    p.set_defaults(func=cmd_migrate_legacy)
    p = sub.add_parser("restore-edition", help="store an earlier edition's "
                       "rows as the next edition (preview by default)")
    p.add_argument("period", type=_year_arg, metavar="YEAR")
    p.add_argument("edition", type=int)
    pe.mode_parser(p)
    p.set_defaults(func=cmd_restore_edition)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
