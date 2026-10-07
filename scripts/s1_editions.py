"""S1 edition history: append-only table of every published edition of each quarter.

la_statutory_homelessness holds one row per LA per quarter (the latest layer).
This module owns la_statutory_homelessness_editions, which keeps every
published edition of a quarter so a revision never overwrites what was sent.
Rows are immutable: a trigger raises on UPDATE, DELETE and TRUNCATE.

Subcommands:
    python scripts/s1_editions.py ddl       # idempotent; safe to run repeatedly
    python scripts/s1_editions.py backfill  # edition 1 of every stored quarter
                                            # plus the 2025Q2 revision; idempotent
    python scripts/s1_editions.py diff --period P --new N --old M
    python scripts/s1_editions.py load --period P
        (--manifest-label registry | --manifest-entry N)
        [--supersedes E] [--published-date YYYY-MM-DD] [--commit | --simulate]
        # DRY-RUN by default: extracts the manifest file, diffs it against the
        # latest stored edition, writes nothing. Only --commit inserts.
        # --manifest-label selects the manifest entry of that period by its
        # release_label (use `registry`); --manifest-entry is a 0-based index
        # into s1_editions_manifest.json. Give exactly one. Only registry
        # entries load unless --allow-non-registry is given. --commit runs
        # gates 6-8 for the period in the same transaction and rolls back on
        # failure; --simulate does the same and always rolls back.
        # --supersedes must equal the period's chain tip.
    python scripts/s1_editions.py status
        # what needs action (new quarter, newer edition not yet in live, live
        # changed outside the editions tables, bad row counts); exit 1 if any
    python scripts/s1_editions.py sync-new [--commit | --simulate]
        # records every live quarter that has no editions as edition 1
        # 'as loaded'; idempotent; DRY-RUN by default
    python scripts/s1_editions.py refresh-latest [--commit | --simulate]
                                                 [--accept-drift PERIOD]
        # copies the latest edition into la_statutory_homelessness for every
        # period whose live rows differ from it (six measures, source_file and
        # extracted_at only; never *_suspect or loaded_at). DRY-RUN by default
        # (prints counts per period). --commit takes a per-period content hash
        # before and after in the same transaction (only the periods being
        # refreshed may change, and only in the refresh columns), re-checks
        # that each refreshed period equals its edition and that the W1
        # outputs moved only where a current-quarter or prior-year TA figure
        # changed; it rolls back on any failure. --simulate does the same and
        # always rolls back. A period whose live rows equal no stored edition
        # is drift (changed outside the editions tables): the command halts
        # unless --accept-drift PERIOD names it.
    python scripts/s1_editions.py load-new --period P
        (--manifest-entry N | --manifest-file NAME)
        [--expected-authorities N] [--commit | --simulate]
        # a quarter that is NOT stored yet (no live rows, no editions): reads
        # the file's own front sheet (Cover, or Contents in the older files)
        # and refuses unless it agrees with the period and with the manifest
        # entry's release_label_actual / published_date_actual; extracts;
        # inserts edition 1 (file-backed), the live rows and the
        # homelessness_quarter_urls row (inserted, or only marked loaded), and
        # runs the gates in the same transaction. DRY-RUN by default; --simulate
        # rolls back; a period that exists halts and points to `load`.
    python scripts/s1_editions.py dryrun-all --out report.md
        # both manifest candidates of 2023Q2-2024Q4 vs stored edition 1 and
        # vs each other, as markdown; writes no table

New-quarter procedure (load-new). The operator adds one object to
scripts/s1_editions_manifest.json with exactly these fields, all required
(load-new halts naming any that is missing):
    period                  e.g. 2026Q1 (financial-year quarter; April to
                            June 2026 is 2026Q1, January to March 2026 is
                            2025Q4)
    file                    <period>_<original name>, the name the file is
                            saved under in data/raw/s1b_a3/ (gitignored)
    url                     where it was published (becomes source_url)
    sha256                  of the saved file, e.g.
                            python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" FILE
                            or sha256sum FILE
    release_label           how the file was found (e.g. 'release page')
    last_modified           the publisher's timestamp, e.g. 2026-10-29T09:30:00Z
    release_label_actual    free text that must contain the release date as
                            printed on the file's own cover, 'D Month YYYY'
                            (e.g. 'April to June 2026 release, released 29
                            October 2026'); it becomes the edition's
                            release_label (unlike 'Original', 'Revised',
                            'registry' or 'as loaded')
    published_date_actual   that same date, ISO yyyy-mm-dd; it becomes the
                            edition's published_date
The loader opens the file, reads its own Cover/Contents sheet and refuses unless
the period and date agree with the entry. Then: `load-new ... ` (dry run, runs
the database gates and rolls back), `--simulate`, then `--commit`.
Independent raw-cell re-reads run inside the transaction and cover all six
stored measures: households_in_ta (TA1 cells), support_needs_total (A3 cells)
and total_assessments, owed_duty, prevention_duty, relief_duty (A1 cells).
What the re-reads share with `extract` (s1_extract_ods.py), and what they do
not. Shared: the column resolution by header text for A1 and TA1
(resolve_columns / COLUMN_LABELS) and the publisher-code recode. Independent:
the classification of each raw cell, which has its own marker table and halts
or fails the gate on a cell of unknown format instead of storing NULL
(classify_a1_cell for A1, classify_ta1_cell for TA1; A3 is read by the S1b
reader, which also has its own header mapping and cell reader). Not covered:
a renamed header that uniquely matches the wrong column would be resolved the
same way by `extract` and by the A1 and TA1 re-reads, so those two would still
pass; only A3 has an independent header mapping.

Helpers imported by later steps: sha256_file, create_schema, _insert (the
only writer of S1 editions), latest_edition, diff_editions, diff_records, and
the table-parameterised latest_map, rows_differing, classify_period, status,
period_hashes, guard_problems, modal_count.

The editions machinery itself is editions_core, driven by SPEC below:
create_schema, the insert, the chain tip, latest_map, status, the refresh
plan and refresh_latest's UPDATE and hash guard. The table-parameterised
helpers keep their signatures (s1b/ro4 and the verify scripts import them)
and are thin adapters over the core. What stays here and why: sync_new and
its 'as loaded' recording (the editions table carries source_url, which the
live table lacks, and the messages and monkeypatch seams are S1's own);
period_hashes (returns (rows, md5); the verify scripts use it as a check
independent of the core's guard); the preview's _unrepairable (no row to
update only, so the preview is unchanged); the W1-equivalence check, plugged
into the core's refresh through its after-update hook; reproduction_update;
load-new and its cover-sheet and raw-cell gates; backfill, diff, load,
dryrun-all and the manifest handling.
"""
import argparse
import hashlib
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
from editions_core import halt, modal_count  # noqa: E402,F401

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_statutory_homelessness_editions"
LIVE = "la_statutory_homelessness"

MEASURES = ("total_assessments", "owed_duty", "prevention_duty", "relief_duty",
            "households_in_ta", "support_needs_total",
            "mental_health_suspect", "learning_disability_suspect",
            "drug_dependency_suspect", "alcohol_dependency_suspect",
            "rough_sleeping_history_suspect")
LIVE_MEASURES = MEASURES[:6]
LIVE_SUSPECT = MEASURES[6:]
# columns refresh-latest may overwrite in the live table; nothing else changes
REFRESH_COLS = LIVE_MEASURES + ("source_file", "extracted_at")

# The editions core's description of S1. Every column of the editions table
# is declared (all eleven measures, and source_url, which the live table
# lacks), with the real types, so create_schema reproduces the table (the
# trigger and function names follow from name 's1'). The refresh writes
# REFRESH_COLS: it compares live with an edition on the six live measures
# (SPEC.compare_cols), writes a row when one of them or source_file differs
# (SPEC.update_cols) and sets extracted_at from the edition's loaded_at. The
# *_suspect columns and loaded_at are never compared or written.
SPEC = core.EditionSpec(
    name="s1",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=("lad24cd",),
    period_col="period",
    key_types=(("period",
                "varchar(6) NOT NULL CHECK (period ~ '^\\d{4}Q[1-4]$')"),),
    value_cols=tuple((m, "integer") for m in MEASURES),
    extra_cols=(("source_url", "text"),),
    refresh_cols=REFRESH_COLS,
    refresh_from=(("extracted_at", "loaded_at"),),
)
TRIGGER = SPEC.trigger                    # s1_editions_immutable
TRUNCATE_TRIGGER = SPEC.truncate_trigger  # s1_editions_no_truncate


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent
    (editions_core.create_schema with SPEC: same table, trigger and function
    names and function body as before; an existing table is left as it is)."""
    core.create_schema(cur, SPEC)


def _insert(cur, recs: list, period: str, *, release_label: str,
            published_date: "date | None", source_url: "str | None",
            source_file: str, source_sha256: str,
            supersedes: "int | None") -> int:
    """Insert one edition of a period; return its edition number. The only
    writer of S1 editions (backfill, load, sync-new, load-new).

    editions_core.insert_edition with SPEC, not strict: S1's original insert
    stored a missing measure as NULL (r.get), so a missing measure is still
    stored NULL. source_url is one value for the whole edition and is put on
    every row here. Every row is stored under `period`, whatever period the
    record itself carries, as S1's original insert did (load-new's
    end-to-end gate extracts a stored quarter's file into a fake quarter;
    the core would refuse a record of another period). If source_sha256 is
    already recorded for the period nothing is inserted and that edition's
    number is returned. Empty recs halts with S1's own message; `supersedes`
    must be the period's chain tip (None for a period with no editions), and
    the chain must be valid."""
    if not recs:
        halt(f"insert_edition: no records supplied for {period}")
    rows = [dict(r, period=period, source_url=source_url) for r in recs]
    return core.insert_edition(
        cur, SPEC, rows, period, release_label=release_label,
        published_date=published_date, source_file=source_file,
        source_sha256=source_sha256, supersedes=supersedes, strict=False)


EDITION_TABLES = (TABLE, "la_homelessness_support_needs_editions",
                  "ro4_housing_expenditure_editions")


def _ident(name: str) -> str:
    """A bare SQL identifier (the period-column name is interpolated)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"not a plain column name: {name!r}")
    return name


def latest_edition(cur, period: str, table: str = TABLE,
                   period_col: str = "period") -> int:
    """Tip of the period's supersedes chain.

    Ordering is the `supersedes` chain, not published_date (which is
    informational only). `table` must be one of EDITION_TABLES (the S1, S1b
    and RO4 editions tables); `period_col` names the column that holds the
    period in that table ('period', or 'financial_year' for RO4). The whole structure is validated, and ValueError is
    raised unless it is one linear chain: exactly one root (supersedes NULL),
    every other edition supersedes a different existing edition, no edition is
    superseded twice (fork), and walking from the root reaches every edition
    (so a cycle or two editions superseding each other is refused). The tip is
    the end of that walk. LookupError if the period has no editions.

    The validator is the core's (_chain_tip, extracted from this function;
    same messages). Raises rather than halting, as before.
    """
    if table not in EDITION_TABLES:
        raise ValueError(f"latest_edition: unknown editions table {table!r}")
    cur.execute(f"SELECT DISTINCT edition, supersedes FROM public.{table} "
                f"WHERE {_ident(period_col)} = %s", (period,))
    return core._chain_tip(cur.fetchall(), period)


def cmd_ddl(_args):
    from _db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            create_schema(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"ddl: {TABLE} and trigger {TRIGGER} present")


REF_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
CSV_MEASURES = MEASURES[:6]
Q2_FILES = (  # (file, published, release_label, supersedes)
    ("statutory_homelessness_2025Q2.csv", date(2026, 2, 26), "Original", None),
    ("statutory_homelessness_2025Q2_revised.csv", date(2026, 4, 30),
     "Revised", 1),
)
LIVE_LABEL = "as loaded; published_date is the load date"


def live_text_sha256(recs: list) -> str:
    """sha256 of a canonical text rendering of one quarter's stored rows.

    Rendering: one line per authority, sorted by lad24cd, of the form
    lad24cd|m1|...|m11 over MEASURES in order, NULL as the empty string,
    lines joined by LF, no trailing newline, UTF-8.
    """
    lines = []
    for r in sorted(recs, key=lambda r: r["lad24cd"]):
        vals = ["" if r.get(m) is None else str(r[m]) for m in MEASURES]
        lines.append("|".join([r["lad24cd"]] + vals))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _csv_recs(path: Path, mapping: dict, current: set) -> list:
    """Read one 2025Q2 CSV: '' -> NULL, publisher codes recoded to canonical."""
    recs = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            code = mapping.get(row["lad24cd"], row["lad24cd"])
            if code not in current:
                halt(f"{path.name}: {row['lad24cd']} is not a known lad24cd")
            rec = {"lad24cd": code}
            for m in CSV_MEASURES:
                v = (row.get(m) or "").strip()
                rec[m] = int(float(v)) if v != "" else None
            recs.append(rec)
    if len({r["lad24cd"] for r in recs}) != len(recs):
        halt(f"{path.name}: duplicate authorities after code resolution")
    return recs


def backfill(cur) -> list:
    """Edition 1 for the ten live quarters, editions 1-2 for 2025Q2.

    Live quarters are skipped when any edition already exists: the live table
    may since have been refreshed, and re-reading it would record a spurious
    'as loaded' edition. Returns a list of (period, edition, rows, inserted).
    """
    from s1_extract_ods import code_resolution
    current, mapping = code_resolution()
    out = []
    # the authority count of a quarter, derived from the live table
    want = expected_authorities(cur)

    def run(period, recs, **kw):
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s", (period,))
        before = cur.fetchone()[0]
        ed = _insert(cur, recs, period, **kw)
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s AND edition = %s", (period, ed))
        n = cur.fetchone()[0]
        cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                    "WHERE period = %s", (period,))
        if n != want:
            halt(f"{period} edition {ed}: {n} rows, expected {want}")
        out.append((period, ed, n, cur.fetchone()[0] > before))

    cur.execute("SELECT DISTINCT period FROM public.la_statutory_homelessness "
                "ORDER BY 1")
    for (period,) in cur.fetchall():
        if period == "2025Q2":
            continue
        cur.execute(f"SELECT 1 FROM public.{TABLE} WHERE period = %s LIMIT 1",
                    (period,))
        if cur.fetchone():
            out.append((period, None, 0, False))
            continue
        cols = ", ".join(("lad24cd",) + MEASURES)
        cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date,
                               source_file
                        FROM public.la_statutory_homelessness
                        WHERE period = %s""", (period,))
        rows = cur.fetchall()
        recs = [dict(zip(("lad24cd",) + MEASURES, r[:-2])) for r in rows]
        loaded = {r[-2] for r in rows}
        files = {r[-1] for r in rows}
        if len(loaded) != 1 or len(files) != 1:
            halt(f"{period}: live rows differ in loaded_at/source_file "
                 f"({len(loaded)} dates, {len(files)} files)")
        run(period, recs, release_label=LIVE_LABEL,
            published_date=loaded.pop(), source_url=None,
            source_file=files.pop(), source_sha256=live_text_sha256(recs),
            supersedes=None)

    for fname, published, label, sup in Q2_FILES:
        path = REF_DIR / fname
        run("2025Q2", _csv_recs(path, mapping, current), release_label=label,
            published_date=published, source_url=None, source_file=fname,
            source_sha256=sha256_file(path), supersedes=sup)
    return out


def cmd_backfill(_args):
    from _db import get_conn
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            create_schema(cur)
            results = backfill(cur)
            cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
            total = cur.fetchone()[0]
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    for period, ed, n, inserted in results:
        print(f"  {period} edition {ed}: "
              + ("inserted" if inserted else "already present"))
    print(f"backfill: {sum(r[3] for r in results)} edition(s) added; "
          f"{TABLE} now holds {total} rows")


MANIFEST = Path(__file__).resolve().parent / "s1_editions_manifest.json"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "s1b_a3"
# The seven quarters restated in the 2026-10 reload. Historical only: the
# one-off dryrun-all reports (here and in s1b_editions) and gates 6-8 of the
# verify scripts use it. The refresh path of both tables derives what to update
# from the data and never reads it.
STALE_PERIODS = ("2023Q2", "2023Q3", "2023Q4", "2024Q1", "2024Q2", "2024Q3",
                 "2024Q4")


def _transition(old, new) -> str:
    if old == 0 and new is None:
        return "zero -> NULL"
    if old is None and new == 0:
        return "NULL -> zero"
    if old is None:
        return "NULL -> value"
    if new is None:
        return "value -> NULL"
    return "value -> value"


def _diff_maps(new_map: dict, old_map: dict) -> dict:
    """Core diff of two {lad24cd: {measure: value}} maps."""
    only_new = sorted(set(new_map) - set(old_map))
    only_old = sorted(set(old_map) - set(new_map))
    changed_auth = set()
    cells = {m: 0 for m in MEASURES}
    transitions = {m: Counter() for m in MEASURES}
    totals = Counter()
    for lad in set(new_map) & set(old_map):
        for m in MEASURES:
            o, n = old_map[lad].get(m), new_map[lad].get(m)
            if o != n:
                cells[m] += 1
                changed_auth.add(lad)
                t = _transition(o, n)
                transitions[m][t] += 1
                totals[t] += 1
    return {
        "authorities_changed": len(changed_auth),
        "cells_changed": cells,
        "no_change": not changed_auth and not only_new and not only_old,
        "transitions": {m: dict(c) for m, c in transitions.items() if c},
        "transition_totals": {t: totals.get(t, 0) for t in (  # not a source value
            "zero -> NULL", "NULL -> zero", "value -> value",
            "NULL -> value", "value -> NULL")},
        "only_in_new": only_new,
        "only_in_old": only_old,
    }


def _edition_map(cur, period, edition) -> dict:
    cols = ", ".join(("lad24cd",) + MEASURES)
    cur.execute(f"SELECT {cols} FROM public.{TABLE} "
                "WHERE period = %s AND edition = %s", (period, edition))
    return {r[0]: dict(zip(MEASURES, r[1:])) for r in cur.fetchall()}


def diff_editions(cur, period: str, new_edition: int, old_edition: int) -> dict:
    """Cell-level diff of two stored editions of one period (read-only).

    Returns authorities_changed, cells_changed {measure: n}, no_change, plus
    transitions per measure and transition_totals ('zero -> NULL',
    'NULL -> zero', 'value -> value', and the two NULL<->value kinds).
    """
    new_map = _edition_map(cur, period, new_edition)
    old_map = _edition_map(cur, period, old_edition)
    if not new_map or not old_map:
        halt(f"diff_editions: {period} edition "
             f"{new_edition if not new_map else old_edition} has no rows")
    return _diff_maps(new_map, old_map)


def diff_records(recs: list, cur, period: str, old_edition: int) -> dict:
    """As diff_editions, but the new side is extracted records not yet stored."""
    old_map = _edition_map(cur, period, old_edition)
    if not old_map:
        halt(f"diff_records: {period} edition {old_edition} has no rows")
    new_map = {r["lad24cd"]: {m: r.get(m) for m in MEASURES} for r in recs}
    return _diff_maps(new_map, old_map)


def load_manifest() -> list:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _extract_entry(entry: dict, expected_rows: "int | None" = None) -> tuple:
    """Extract one manifest file explicitly; verify its sha256. -> (recs, path).

    expected_rows is the authority count derived from the data (see
    expected_authorities); when given, the file must have exactly that many
    distinct authorities. Without it only uniqueness is checked."""
    from s1_extract_ods import extract
    path = RAW_DIR / entry["file"]
    if not path.exists():
        halt(f"{path} not found")
    sha = sha256_file(path)
    if sha != entry["sha256"]:
        halt(f"{entry['file']}: sha256 {sha} does not match manifest "
             f"{entry['sha256']}")
    label = ("MHCLG Statutory Homelessness Detailed Local Authority Data, "
             + entry["file"])
    recs = extract(path, entry["period"], label)
    n = len({r["lad24cd"] for r in recs})
    if n != len(recs) or (expected_rows is not None and n != expected_rows):
        halt(f"{entry['file']}: {len(recs)} rows/{n} authorities extracted, "
             f"expected {expected_rows if expected_rows is not None else 'unique'}")
    return recs, path


def format_diff(d: dict) -> str:
    cells = ", ".join(f"{m}={n}" for m, n in d["cells_changed"].items() if n)
    t = ", ".join(f"{k}={v}" for k, v in d["transition_totals"].items() if v)
    return (f"authorities_changed={d['authorities_changed']}; "
            f"no_change={d['no_change']}; cells: {cells or 'none'}; "
            f"transitions: {t or 'none'}"
            + (f"; only_in_new={d['only_in_new']}" if d["only_in_new"] else "")
            + (f"; only_in_old={d['only_in_old']}" if d["only_in_old"] else ""))


def cmd_diff(args):
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            d = diff_editions(cur, args.period, args.new, args.old)
    finally:
        conn.close()
    print(f"{args.period} edition {args.new} vs {args.old}: {format_diff(d)}")


def check_registry(cur, period, edition, known_codes=None,
                   require_registry=True, expected_rows=None) -> list:
    """Gate 6 for one edition: the derived authority count (the most common
    per-period live row count, or expected_rows) as authorities and rows, no lad24cd outside
    la_code_lookup (or known_codes, for tests), a source_url, and the
    'registry' label when require_registry. Returns a list of problems."""
    cur.execute(f"""SELECT lad24cd, release_label, source_url FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    rows = cur.fetchall()
    if known_codes is None:
        cur.execute("SELECT new_code FROM public.la_code_lookup UNION "
                    "SELECT old_code FROM public.la_code_lookup")
        known_codes = {r[0] for r in cur.fetchall()}
    tag = f"{period} ed{edition}"
    bad = []
    if expected_rows is None:
        expected_rows = expected_authorities(cur)
    n = len({r[0] for r in rows})
    if (n, len(rows)) != (expected_rows, expected_rows):
        bad.append(f"{tag}: {n} authorities/{len(rows)} rows, expected "
                   f"{expected_rows}/{expected_rows}")
    orphans = sorted({r[0] for r in rows} - set(known_codes))
    if orphans:
        bad.append(f"{tag}: {len(orphans)} lad24cd outside la_code_lookup "
                   f"{orphans[:3]}")
    if any(r[2] is None for r in rows):
        bad.append(f"{tag}: source_url missing")
    if require_registry and {r[1] for r in rows} != {"registry"}:
        bad.append(f"{tag}: release_label {sorted({str(r[1]) for r in rows})},"
                   " expected registry")
    return bad


def raw_ta1(path) -> dict:
    """lad24cd -> (published TA1 cell text, extractor value) for one file."""
    from s1_extract_ods import (LA_CODE, COLUMN_LABELS, code_resolution, num,
                                read_sheets, resolve_columns)
    rows = read_sheets(path, {"TA1"})["TA1"]
    j = resolve_columns(rows, COLUMN_LABELS["TA1"], "TA1")["households_in_ta"]
    _, recode = code_resolution()
    out = {}
    for row in rows:
        code = (row[0] or "").strip() if row else ""
        if LA_CODE.fullmatch(code):
            cell = row[j] if j < len(row) else ""
            out[recode.get(code, code)] = (cell, num(cell))
    return out


def edition_source_path(cur, period, edition):
    """(raw file path or None, files) for a stored edition's source_file."""
    cur.execute(f"""SELECT DISTINCT source_file FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    files = [r[0] for r in cur.fetchall()]
    path = (RAW_DIR / files[0] if len(files) == 1 and files[0] is not None
            else None)
    return (path if path is not None and path.exists() else None), files


# the TA1 re-read's own marker table (not shared with the extractor or A1)
TA1_MARKERS = frozenset(("", "-", "..", ":", "*", "x", "[x]", "[c]", "[z]",
                         "[low]", "n/a"))


def classify_ta1_cell(cell) -> tuple:
    """('suppressed', None) for a marker or blank, ('number', n) for a plain
    numeric cell, else ('unrecognised', None): a value in any other format
    (e.g. '123[r]') is reported, never turned into NULL. Independent of the
    extractor's num."""
    t = str(cell if cell is not None else "").strip().lower()
    if t in TA1_MARKERS:
        return "suppressed", None
    try:
        return "number", int(round(float(t.replace(",", ""))))
    except ValueError:
        return "unrecognised", None


def check_no_suppressed_zero(cur, period, edition, raw=None) -> tuple:
    """Gate 7 for one edition (households_in_ta, TA1 cells). raw (lad ->
    (cell, value); only the cell text is used) defaults to a re-read of the
    edition's source file. Each stored value must equal classify_ta1_cell of
    the published cell: a marker is NULL, a number is that number (so a
    stored 0 needs a published 0), and a cell in an unknown format is a
    problem. -> (problems, rows, zeros)."""
    tag = f"{period} ed{edition}"
    if raw is None:
        path, files = edition_source_path(cur, period, edition)
        if path is None:
            return [f"{tag}: source file {files} not found in raw dir"], 0, 0
        raw = raw_ta1(path)
    cur.execute(f"""SELECT lad24cd, households_in_ta FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    bad, n, zeros = [], 0, 0
    for lad, stored in cur.fetchall():
        n += 1
        if lad not in raw:
            bad.append(f"{tag} {lad}: no TA1 row in the file")
            continue
        cell, _ = raw[lad]
        kind, val = classify_ta1_cell(cell)
        if stored == 0:
            zeros += 1
        if kind == "unrecognised":
            bad.append(f"{tag} {lad}: unrecognised TA1 cell {cell!r} "
                       f"(stored {stored})")
        elif stored == 0 and val != 0:
            bad.append(f"{tag} {lad}: stored 0, TA1 cell {cell!r}")
        elif stored != val:
            bad.append(f"{tag} {lad}: stored {stored}, re-extracted {val}")
    return bad, n, zeros


def a3_expected(cur, path) -> dict:
    """lad24cd -> A3 'households with one or more support needs', read with
    the S1b reader (read_a3, map_columns, la_rows, cell, resolve_lookup):
    these only parse the workbook and read la_code_lookup, so they are safe
    to run on any file."""
    import s1b_support_needs_build as s1b
    df = s1b.read_a3(path)
    _, mapping, _ = s1b.map_columns(df)
    col = [j for j, c in mapping.items()
           if c == "hh_one_or_more_support_needs"][0]
    rows = s1b.la_rows(df)
    resolved, unresolved = s1b.resolve_lookup(cur, rows)
    if unresolved:
        halt(f"{path.name}: unresolved publisher codes {unresolved}")
    return {resolved[code]: s1b.cell(df.iat[i, col])[0]
            for code, i in rows.items()}


def check_support_needs(cur, period, edition, expected=None,
                        expected_rows=None) -> list:
    """Gate 8 for one edition: support_needs_total equals the independent
    S1b-reader A3 re-read of the same source file for every authority (the
    derived per-period count)."""
    tag = f"{period} ed{edition}"
    if expected is None:
        path, files = edition_source_path(cur, period, edition)
        if path is None:
            return [f"{tag}: source file {files} not found in raw dir"]
        expected = a3_expected(cur, path)
    cur.execute(f"""SELECT lad24cd, support_needs_total FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    stored = dict(cur.fetchall())
    n = sum(1 for lad, v in stored.items() if expected.get(lad, "x") == v)
    if expected_rows is None:
        expected_rows = expected_authorities(cur)
    if n != expected_rows or len(stored) != expected_rows:
        return [f"{tag}: {n}/{expected_rows} equal ({len(stored)} stored rows)"]
    return []


def select_entry(manifest, period, label=None, index=None) -> dict:
    """Manifest entry chosen by release_label or 0-based index, never both."""
    if (label is None) == (index is None):
        halt("give exactly one of --manifest-label and --manifest-entry")
    if label is not None:
        hits = [e for e in manifest
                if e["period"] == period and e["release_label"] == label]
        if len(hits) != 1:
            halt(f"{len(hits)} manifest entries for {period} with "
                 f"release_label {label!r}, expected exactly 1")
        return hits[0]
    if not 0 <= index < len(manifest):
        halt(f"--manifest-entry {index} outside 0..{len(manifest) - 1}")
    if manifest[index]["period"] != period:
        halt(f"manifest entry {index} is {manifest[index]['period']}, "
             f"not {period}")
    return manifest[index]


def run_load_gates(cur, period, edition) -> None:
    """Gates 6-8 for the edition just inserted; halt on any problem."""
    problems = check_registry(cur, period, edition)
    p7, _, _ = check_no_suppressed_zero(cur, period, edition)
    problems += p7
    problems += check_support_needs(cur, period, edition)
    if problems:
        halt(f"{period} edition {edition} failed gates 6-8, rolled back: "
             + "; ".join(problems[:5]))


def cmd_load(args):
    entry = select_entry(load_manifest(), args.period,
                         args.manifest_label, args.manifest_entry)
    if entry["release_label"] != "registry" and not args.allow_non_registry:
        halt(f"{entry['file']} has release_label {entry['release_label']!r}; "
             "only registry files load as editions (--allow-non-registry "
             "overrides)")
    recs, path = _extract_entry(entry)
    published = (date.fromisoformat(args.published_date) if args.published_date
                 else date.fromisoformat(entry["last_modified"][:10]))
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                        "WHERE period = %s AND source_sha256 = %s",
                        (args.period, entry["sha256"]))
            have = [r[0] for r in cur.fetchall()]
            if have:
                print(f"already loaded (edition {have[0]}), nothing inserted")
                conn.rollback()
                return
            want = expected_authorities(cur)
            if len(recs) != want or len({r["lad24cd"] for r in recs}) != want:
                halt(f"{entry['file']}: {len(recs)} authorities extracted, "
                     f"expected {want} (the live table's derived count)")
            try:
                prev = latest_edition(cur, args.period)
            except (LookupError, ValueError) as e:
                halt(f"cannot determine latest edition: {e}")
            if args.supersedes is not None and args.supersedes != prev:
                halt(f"--supersedes {args.supersedes} is not the current "
                     f"chain tip (edition {prev}) of {args.period}")
            d = diff_records(recs, cur, args.period, prev)
            print(f"{args.period} {entry['file']} (published {published}) "
                  f"vs stored edition {prev}:\n  {format_diff(d)}")
            if d["no_change"]:
                print("  NOTE: identical in values to the previous edition; "
                      "it would be recorded as a no_change edition, not a "
                      "revision")
            if not writing:
                print("DRY RUN: nothing written (use --commit to insert)")
                return
            ed = _insert(
                cur, recs, args.period, release_label=entry["release_label"],
                published_date=published, source_url=entry["url"],
                source_file=entry["file"], source_sha256=entry["sha256"],
                supersedes=prev)
            if latest_edition(cur, args.period) != ed:
                halt(f"{args.period}: new edition {ed} is not the chain tip")
            run_load_gates(cur, args.period, ed)
        if args.commit:
            conn.commit()
            print(f"loaded {args.period} edition {ed} ({len(recs)} rows); "
                  "gates 6-8 passed in the same transaction")
        else:
            conn.rollback()
            print(f"SIMULATION: {args.period} edition {ed} inserted, gates "
                  "6-8 passed, ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


def _s1b_needs(cur, period) -> dict:
    """lad24cd -> S1b 'households with one or more support needs' (A3)."""
    cur.execute("SELECT lad24cd, value FROM public.la_homelessness_support_needs "
                "WHERE period = %s AND "
                "category_code = 'hh_one_or_more_support_needs'", (period,))
    return dict(cur.fetchall())


def _needs_match(vals: dict, s1b: dict) -> tuple:
    """(equal, differ) authority counts; NULL equals NULL."""
    eq = sum(1 for lad, v in vals.items() if s1b.get(lad) == v)
    return eq, len(vals) - eq


def _fmt_cells(d):
    return ", ".join(f"{k}={v}" for k, v in d["cells_changed"].items() if v) \
        or "none"


def _fmt_trans(d):
    return ", ".join(f"{k}={v}" for k, v in d["transition_totals"].items()
                     if v) or "none"


def cmd_dryrun_all(args):
    from _db import get_readonly_conn
    manifest = load_manifest()
    out = ["# Task 4a dry-run: manifest candidates vs stored edition 1", "",
           "Generated by `s1_editions.py dryrun-all`. Nothing written to any "
           "table.", "",
           "A = release-page attachment, B = registry file (the manifest's two "
           "candidates per period). Gate-8 feed: authorities (of the derived count) where "
           "extracted `support_needs_total` equals S1b "
           "`la_homelessness_support_needs` category "
           "`hh_one_or_more_support_needs` (A3 'households with one or more "
           "support needs'; NULL equals NULL).", ""]
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for period in STALE_PERIODS:
                ents = [e for e in manifest if e["period"] == period]
                if len(ents) != 2:
                    halt(f"{period}: {len(ents)} manifest entries, expected 2")
                s1b = _s1b_needs(cur, period)
                cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                            "WHERE period = %s", (period,))
                eds = sorted(r[0] for r in cur.fetchall())
                cur.execute("SELECT DISTINCT source_edition FROM "
                            "public.la_homelessness_support_needs "
                            "WHERE period = %s", (period,))
                s1b_src = [r[0] for r in cur.fetchall()]
                e1 = _edition_map(cur, period, 1)
                eq1, _ = _needs_match({k: v["support_needs_total"]
                                       for k, v in e1.items()}, s1b)
                out += [f"## {period}", "",
                        f"Stored editions: {eds}. S1b source file: {s1b_src}. "
                        f"Stored edition 1 support_needs_total equals S1b for "
                        f"{eq1}/{len(e1)}.", ""]
                got = {}
                for tag, e in zip("AB", ents):
                    recs, _ = _extract_entry(e, len(e1))
                    got[tag] = recs
                    d = diff_records(recs, cur, period, 1)
                    m = {r["lad24cd"]: r.get("support_needs_total")
                         for r in recs}
                    eq, ne = _needs_match(m, s1b)
                    out += [f"### Candidate {tag} vs edition 1: `{e['file']}` "
                            f"({e['release_label']}, last_modified "
                            f"{e['last_modified']})", "",
                            f"- authorities changed: "
                            f"{d['authorities_changed']} of {len(e1)}; "
                            f"no_change: {d['no_change']}",
                            f"- cells changed: {_fmt_cells(d)}",
                            f"- transitions (all measures): {_fmt_trans(d)}",
                            "- households_in_ta transitions: "
                            f"{d['transitions'].get('households_in_ta', 'none')}",
                            "- gate-8 feed: support_needs_total equals S1b for "
                            f"{eq}/{len(e1)} authorities ({ne} differ)", ""]
                ma = {r["lad24cd"]: {m: r.get(m) for m in MEASURES}
                      for r in got["A"]}
                mb = {r["lad24cd"]: {m: r.get(m) for m in MEASURES}
                      for r in got["B"]}
                dab = _diff_maps(mb, ma)
                out += ["### A vs B pairwise (B against A)", "",
                        f"- values identical: {dab['no_change']}",
                        f"- authorities differing: {dab['authorities_changed']}"
                        f"; cells: {_fmt_cells(dab)}",
                        f"- transitions: {_fmt_trans(dab)}", ""]
    finally:
        conn.close()
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}")


W1_DIR = Path(__file__).resolve().parent / "verify" / "w1_live"
# signals columns that legitimately move when a revised households_in_ta
# reaches the current quarter or the prior-year quarter; data_quality is
# derived from them
TA_SIGNAL_COLS = {"ta_households_current", "ta_households_prev_year",
                  "ta_yoy_pct", "ta_trend_label", "data_quality"}
NATIONAL_TA_COLS = {"ta_households_current", "ta_households_prev_year",
                    "ta_yoy_pct"}


# ---------------------------------------------------------------------------
# Generic pieces (parameterised by table and column names; the S1b and RO4
# modules and the verify scripts import them): thin adapters over
# editions_core, with S1's signatures and S1's status keys. Called with S1's
# own tables and columns they run on SPEC itself.
# ---------------------------------------------------------------------------

def _spec_for(live=LIVE, editions=TABLE, key_cols=("lad24cd",),
              cols=LIVE_MEASURES, period_col="period",
              expected_rows=None) -> "core.EditionSpec":
    """SPEC when the arguments are S1's own; otherwise a spec for the core's
    read-side functions with `cols` as the compared columns. It is never
    used to create or write a table."""
    if (live, editions, tuple(key_cols), tuple(cols), period_col,
            expected_rows) == (LIVE, TABLE, ("lad24cd",), LIVE_MEASURES,
                               "period", None):
        return SPEC
    return core.EditionSpec(
        name="shared", live_table=live, editions_table=editions,
        key_cols=tuple(key_cols), period_col=_ident(period_col),
        value_cols=tuple((c, "integer") for c in cols),
        refresh_cols=tuple(cols), fk_la_boundaries=False,
        expected_rows_per_period=(None if expected_rows is None else
                                  lambda _cur, p: expected_rows.get(p)))


def latest_map(cur, live=LIVE, editions=TABLE, period_col="period") -> tuple:
    """(tips, new_periods, chain_errors) for the periods present in `live`.

    tips: {period: chain-tip edition}; new_periods: periods with no editions;
    chain_errors: {period: message} where the chain is invalid
    (editions_core.latest_map, which also halts on a NULL live period).
    """
    return core.latest_map(cur, _spec_for(live, editions,
                                          period_col=period_col))


def rows_differing(cur, live, editions, key_cols, cols, period, edition,
                   period_col="period") -> int:
    """Rows of `period` that are in only one of the live table and the given
    edition, compared on key_cols + cols (NULL equals NULL;
    editions_core.rows_differing, EXCEPT ALL)."""
    return core.rows_differing(
        cur, _spec_for(live, editions, key_cols, cols, period_col),
        period, edition)


def classify_period(cur, live, editions, key_cols, cols, period, tip,
                    period_col="period") -> tuple:
    """('current'|'pending'|'drift', matched_edition).

    current: live equals the chain tip. pending: live differs from the tip but
    equals an earlier edition, i.e. a newer edition has been recorded and not
    yet refreshed. drift: live differs from the tip and equals no stored
    edition, so it was changed outside the editions machinery
    (editions_core.classify_period)."""
    return core.classify_period(
        cur, _spec_for(live, editions, key_cols, cols, period_col),
        period, tip)


def live_period_counts(cur, live=LIVE, period_col="period") -> dict:
    return core.live_period_counts(cur, _spec_for(live,
                                                  period_col=period_col))


def expected_authorities(cur, live=LIVE) -> "int | None":
    return modal_count(live_period_counts(cur, live))


def status(cur, table_live=LIVE, table_editions=TABLE, key_cols=("lad24cd",),
           cols=LIVE_MEASURES, expected_rows=None, period_col="period") -> dict:
    """What needs action between the live table and its editions (read-only).

    new_periods:     in live with no editions (run sync-new)
    drift_periods:   live differs from the latest edition and equals no stored
                     edition (changed outside the editions machinery)
    pending_refresh: live equals an earlier edition while a later one exists
                     (run refresh-latest)
    chain_errors:    {period: message} where the supersedes chain is invalid
    bad_counts:      {period: (live rows, expected)} where the row count is not
                     the one derived from the data (or edition rows differ)
    ok is true only when all five are empty. expected_rows: optional
    {period: n} override; default is the most common per-period live count.

    editions_core.status, with its keys mapped back to S1's names (drift ->
    drift_periods, forked -> chain_errors) so format_status, the callers
    and the verify gates read the same dict.
    """
    st = core.status(cur, _spec_for(table_live, table_editions, key_cols,
                                    cols, period_col, expected_rows))
    return {"new_periods": st["new_periods"], "drift_periods": st["drift"],
            "pending_refresh": st["pending_refresh"],
            "chain_errors": st["forked"], "bad_counts": st["bad_counts"],
            "periods": st["periods"], "ok": st["ok"]}


def format_status(st: dict, label: str) -> str:
    lines = [f"{label}: {st['periods']} periods in the live table"]
    if st["new_periods"]:
        lines.append("  NEW, no editions recorded (run sync-new): "
                     + ", ".join(st["new_periods"]))
    if st["pending_refresh"]:
        lines.append("  NEWER EDITION NOT YET IN LIVE (run refresh-latest): "
                     + ", ".join(st["pending_refresh"]))
    if st["drift_periods"]:
        lines.append("  DRIFT, live differs from the latest edition and "
                     "matches none (changed outside the editions tables; "
                     "load it as an edition or accept the overwrite with "
                     "refresh-latest --accept-drift PERIOD): "
                     + ", ".join(st["drift_periods"]))
    for p, msg in st["chain_errors"].items():
        lines.append(f"  CHAIN ERROR {p}: {msg}")
    for p, (n, want) in st["bad_counts"].items():
        lines.append(f"  ROW COUNT {p}: {n} rows, expected {want}")
    lines.append("  status: " + ("OK, nothing to do" if st["ok"]
                                 else "ACTION NEEDED"))
    return "\n".join(lines)


def period_hashes(cur, table, key_cols, exclude=("loaded_at",),
                  period_col="period") -> dict:
    """{period: (row count, md5)} of each period's content, rows ordered by
    key_cols, every column except `exclude` (as jsonb text, so NULLs, types
    and column names all count).

    Kept here, not taken from the core: it returns (rows, md5) where the
    core's returns md5 only, and the S1 and S1b verify scripts use it as a
    check independent of the core's own refresh guard."""
    ex = "ARRAY[" + ", ".join(f"'{c}'" for c in exclude) + "]::text[]"
    order = ", ".join(f"t.{c}" for c in key_cols)
    cur.execute(f"""SELECT {_ident(period_col)}, COUNT(*),
        md5(string_agg((to_jsonb(t) - {ex})::text, E'\n' ORDER BY {order}))
        FROM public.{table} t GROUP BY 1""")
    return {p: (n, h) for p, n, h in cur.fetchall()}


def guard_problems(full_before, full_after, kept_before, kept_after,
                   intended) -> list:
    """The before/after rule of a refresh. full_*: period_hashes excluding
    loaded_at only. kept_*: period_hashes excluding the refresh columns (so
    loaded_at and every other column count). A period outside `intended`
    must be identical in full and in loaded_at; a period inside it may differ
    only in the refresh columns (editions_core.guard_problems)."""
    return core.guard_problems(full_before, full_after, kept_before,
                               kept_after, intended)


# ---------------------------------------------------------------------------
# S1 specifics
# ---------------------------------------------------------------------------

def _record_as_loaded(cur, period, expected) -> int:
    """Edition 1 'as loaded' for a live period that has no editions. The live
    rows must share one loaded_at date and one source_file, and number
    `expected`; otherwise halt. Returns the edition number."""
    cols = ", ".join(("lad24cd",) + MEASURES)
    cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date,
                           source_file
                    FROM public.{LIVE} WHERE period = %s""", (period,))
    rows = cur.fetchall()
    if expected is not None and len(rows) != expected:
        halt(f"{period}: {len(rows)} live rows, expected {expected}")
    recs = [dict(zip(("lad24cd",) + MEASURES, r[:-2])) for r in rows]
    loaded = {r[-2] for r in rows}
    files = {r[-1] for r in rows}
    if len(loaded) != 1 or len(files) != 1:
        halt(f"{period}: live rows differ in loaded_at/source_file "
             f"({len(loaded)} dates, {len(files)} files)")
    return _insert(
        cur, recs, period, release_label=LIVE_LABEL,
        published_date=loaded.pop(), source_url=None,
        source_file=files.pop(), source_sha256=live_text_sha256(recs),
        supersedes=None)


def sync_new(cur, expected_authorities_n=None) -> list:
    """Give every live period that has no editions its edition 1, 'as loaded'
    (same canonical-text sha256 as backfill). Never touches a period that
    already has editions, so it is idempotent. Halts on a period whose row
    count is not the one derived from the other periods. Returns the periods
    that were given an edition. If no live period has editions yet there is
    nothing to derive the count from, so expected_authorities_n (CLI
    --expected-authorities N) must be given."""
    tips, new, errors = latest_map(cur)
    if errors:
        halt("sync-new: invalid edition chain " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    counts = live_period_counts(cur)
    known = {p: n for p, n in counts.items() if p not in new}
    derived = modal_count(known)
    if (expected_authorities_n is not None and derived is not None
            and expected_authorities_n != derived):
        halt(f"sync-new: --expected-authorities {expected_authorities_n} "
             f"differs from the count {derived} derived from periods that "
             "already have editions; it is only accepted when nothing can be "
             "derived (or when it equals the derived count)")
    expected = derived if derived is not None else expected_authorities_n
    if new and expected is None:
        halt("sync-new: no live period has editions, so the authority count "
             "cannot be derived; give --expected-authorities N")
    done = []
    for p in new:
        _record_as_loaded(cur, p, expected)
        done.append(p)
    return done


def _plan(cur, accept_drift=()) -> tuple:
    """({period: {edition, kind, rows, one_sided}}, unaccepted drift periods)
    (editions_core._plan with SPEC: the same halts and messages; rows counts
    the live rows whose six measures or source_file differ, as before)."""
    return core._plan(cur, SPEC, accept_drift)


def _unrepairable(plan) -> list:
    """Planned periods that differ from their edition but have no row to
    update (rows missing from, or extra in, the live table): an UPDATE of the
    refresh columns cannot repair them. Kept for the refresh-latest preview
    so its output is unchanged; refresh_latest itself uses the core's
    stricter rule (also any key in only one of live and the edition)."""
    return sorted(p for p, v in plan.items() if not v["rows"])


def refresh_counts(cur, accept_drift=()) -> dict:
    """{period: rows refresh_latest would write} (read-only)."""
    return core.refresh_counts(cur, SPEC, accept_drift)


def _w1_select(filename, table) -> str:
    """The SELECT of a W1 node query, runnable read-only against `table`."""
    sql = (W1_DIR / filename).read_text(encoding="utf-8")
    i = sql.index("SELECT\n    $1 AS run_id")
    sql = sql[i:]
    j = sql.find("\nON CONFLICT (")
    if j != -1:
        sql = sql[:j]
    sql = sql.replace("$1", "0").rstrip().rstrip(";")
    return re.sub(r"\bla_statutory_homelessness\b", table, sql)


def w1_signals(cur, table=LIVE) -> tuple:
    """(column names, {lad24cd: row}) of the W1 la_signals SELECT on `table`."""
    cur.execute(_w1_select("la_signals.sql", table))
    cols = [d[0] for d in cur.description]
    k = cols.index("lad24cd")
    rows = {r[k]: r for r in cur.fetchall()}
    return cols, rows


def w1_national(cur, table=LIVE) -> dict:
    head = (W1_DIR / "national_aggregates.sql").read_text(encoding="utf-8")
    cols = [c.strip() for c in re.search(
        r"INSERT INTO staging_national \((.*?)\)", head, re.S).group(1)
        .split(",")]
    cur.execute(_w1_select("national_aggregates.sql", table))
    row = cur.fetchone()
    if len(row) != len(cols):
        halt("national_aggregates.sql: column list and SELECT disagree")
    return dict(zip(cols, row))


def w1_snapshot(cur) -> dict:
    """W1 signals and national outputs on the live table, plus households_in_ta
    for the current and prior-year quarters (what the W1 queries read)."""
    cols, signals = w1_signals(cur)
    cur.execute(f"SELECT MAX(period) FROM public.{LIVE}")
    top = cur.fetchone()[0]
    prev = f"{int(top[:4]) - 1}{top[4:]}"
    cur.execute(f"""SELECT period, lad24cd, households_in_ta FROM public.{LIVE}
                    WHERE period IN (%s, %s)""", (top, prev))
    ta = {(p, lad): v for p, lad, v in cur.fetchall()}
    return {"cols": cols, "signals": signals, "national": w1_national(cur),
            "ta": ta, "top": top, "prev": prev}


def check_w1_equivalence(before: dict, after: dict) -> tuple:
    """The W1 equivalence gate: signals and national outputs computed before
    and after a refresh differ only in TA-derived columns, and only for
    authorities whose current-quarter or prior-year households_in_ta changed.
    -> (problems, summary)."""
    changed_las = {lad for (p, lad), v in after["ta"].items()
                   if before["ta"].get((p, lad)) != v}
    changed_las |= {lad for (p, lad) in before["ta"]
                    if (p, lad) not in after["ta"]}
    cols = before["cols"]
    bad, differing, colhits = [], {}, {}
    if set(before["signals"]) != set(after["signals"]):
        bad.append("signals authority sets differ")
    for lad in sorted(set(before["signals"]) & set(after["signals"])):
        diff = [c for c, x, y in zip(cols, before["signals"][lad],
                                     after["signals"][lad]) if x != y]
        if not diff:
            continue
        differing[lad] = diff
        for c in diff:
            colhits[c] = colhits.get(c, 0) + 1
        out = [c for c in diff if c not in TA_SIGNAL_COLS]
        if out:
            bad.append(f"{lad}: non-TA signal columns changed {out}")
        if lad not in {x for x in changed_las}:
            bad.append(f"{lad}: signals changed but its current-quarter and "
                       "prior-year TA figures did not")
    nb, na = before["national"], after["national"]
    ndiff = [c for c in nb if nb[c] != na[c]]
    out = [c for c in ndiff if c not in NATIONAL_TA_COLS]
    if out:
        bad.append(f"national aggregates changed outside TA: {out}")
    if ndiff and not changed_las:
        bad.append("national aggregates changed but no TA figure did")
    return bad, {"authorities_differing": sorted(differing),
                 "columns": colhits, "national_before": nb,
                 "national_after": na, "national_columns_changed": ndiff,
                 "ta_changed_authorities": sorted(changed_las)}


def refresh_latest(cur, accept_drift=(), _after_update_hook=None) -> dict:
    """Copy the latest edition into the live table for every period whose live
    rows differ from it (NULL-safe, six measures); return a result dict.

    Writes only the six measures, source_file and extracted_at (= the
    edition's load time), on the rows of those periods that differ. *_suspect
    and loaded_at are never written. Inside the caller's transaction: the
    per-period content hash (excluding loaded_at) is taken before and after;
    a period not being refreshed must be identical, a refreshed one may differ
    only in the refresh columns; each refreshed period must then equal its
    edition; and the W1 outputs may differ only where a current-quarter or
    prior-year TA figure changed. Any failure halts (the caller rolls back).
    A period whose live rows equal no stored edition is drift: it halts unless
    named in accept_drift. _after_update_hook(cur) is a test seam that runs
    after the UPDATE and before the after-checks.

    Result: {'updated': {period: rows}, 'rows': n, 'drift_accepted': [...],
    'w1': summary or None, 'reproduction': [...]}.

    The plan, UPDATE, hash guard and after-checks are
    editions_core.refresh_latest with SPEC (in the core's own savepoint,
    which it rolls back if they fail). S1's part: the W1 'before' snapshot
    is taken here, before the core is called, when there is something to
    refresh; the W1 'after' snapshot and the equivalence check run in the
    core's after-update hook (after the test seam above), and their problems
    halt here once the core's own checks have passed, so a guard failure is
    still reported first. reproduction_update then runs in the caller's
    transaction, as before.
    """
    plan, drift = _plan(cur, accept_drift)
    w1 = {"before": None}
    if plan and not drift and not core._unrepairable(plan):
        w1["before"] = w1_snapshot(cur)

    def hook(c, _core_plan):
        if _after_update_hook is not None:
            _after_update_hook(c)
        if w1["before"] is None:
            halt("refresh-latest: the W1 'before' snapshot was not taken")
        w1["bad"], w1["summary"] = check_w1_equivalence(w1["before"],
                                                        w1_snapshot(c))

    result = core.refresh_latest(cur, SPEC, accept_drift,
                                 _after_update_hook=hook)
    result["w1"], result["reproduction"] = None, []
    if "summary" not in w1:
        return result
    if w1["bad"]:
        halt("refresh-latest failed its before/after checks, rolled back: "
             + "; ".join([f"W1: {x}" for x in w1["bad"]][:6]))
    result["w1"] = w1["summary"]
    result["reproduction"] = reproduction_update(cur, sorted(result["updated"]))
    return result


def reproduction_verdict(diff_cells, file_url, source_url) -> tuple:
    """(reproduces, url_note) for one refreshed period.

    The verdict rests on the cell-by-cell comparison with the edition's
    recorded file only: reproduces = 0 cells differ. A file_url in
    homelessness_quarter_urls that differs from the edition's source_url is
    not a failure (the table holds the release-page link, an edition may come
    from a registry file); it is returned as a note to print and record."""
    note = ("" if file_url == source_url else
            f"; file_url differs from the edition's source_url ({source_url})"
            " (noted, not a failure)")
    return diff_cells == 0, note


def reproduction_update(cur, periods) -> list:
    """Re-evaluate homelessness_quarter_urls for the periods just refreshed.

    'Reproduces from source' means: the live layer now equals the file named
    in the period's latest edition. The edition's file is re-extracted and
    compared cell by cell with the live six measures; reproduces_from_source is
    true only when 0 cells differ. A file_url that differs from the edition's
    source_url is printed and recorded in the note, not treated as a failure
    (reproduction_verdict). reproduction_checked_at = now(); reproduction_diff_cells =
    cells differing; reproduction_note states the result and keeps the earlier
    note text. A period whose edition file is not in the manifest, or that has
    no homelessness_quarter_urls row, is reported as skipped (None) rather
    than guessed. Returns [(period, diff_cells or None, reproduces or None)].
    """
    manifest = {e["file"]: e for e in load_manifest()}
    out = []
    for period in periods:
        ed = latest_edition(cur, period)
        cur.execute(f"""SELECT DISTINCT source_file, source_url FROM
                        public.{TABLE} WHERE period = %s AND edition = %s""",
                    (period, ed))
        (fname, url), = cur.fetchall()
        cur.execute("""SELECT file_url, reproduction_note FROM
                       public.homelessness_quarter_urls WHERE period = %s""",
                    (period,))
        urls_row = cur.fetchone()
        if fname not in manifest or urls_row is None:
            out.append((period, None, None))
            continue
        recs, _ = _extract_entry(manifest[fname], expected_authorities(cur))
        file_map = {r["lad24cd"]: r for r in recs}
        cols = ", ".join(("lad24cd",) + LIVE_MEASURES)
        cur.execute(f"SELECT {cols} FROM public.{LIVE} WHERE period = %s",
                    (period,))
        diff = 0
        for row in cur.fetchall():
            f = file_map.get(row[0])
            for m, v in zip(LIVE_MEASURES, row[1:]):
                if f is None or f.get(m) != v:
                    diff += 1
        file_url, old_note = urls_row
        repro, url_note = reproduction_verdict(diff, file_url, url)
        if url_note:
            print(f"NOTE {period}{url_note}")
        note = (f"Live layer re-extracted against {fname} (edition {ed}, "
                f"{'edition file' if repro else 'check failed'}): {diff} "
                "cells of the six S1 measures differ"
                + url_note
                + f". Checked {date.today().isoformat()} by s1_editions.py "
                f"refresh-latest. Earlier note: {old_note}")
        cur.execute("""UPDATE public.homelessness_quarter_urls
                       SET reproduces_from_source = %s,
                           reproduction_checked_at = now(),
                           reproduction_diff_cells = %s,
                           reproduction_note = %s WHERE period = %s""",
                    (repro, diff, note, period))
        out.append((period, diff, repro))
    return out


def cmd_status(_args):
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            st = status(cur)
    finally:
        conn.close()
    print(format_status(st, "S1 statutory homelessness"))
    sys.exit(0 if st["ok"] else 1)


def cmd_sync_new(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            tips, new, errors = latest_map(cur)
            print("periods with no editions: " + (", ".join(new) or "none"))
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return
            done = sync_new(cur, args.expected_authorities)
            for p in done:
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                            "WHERE period = %s AND edition = 1", (p,))
                print(f"  {p}: edition 1 recorded, {cur.fetchone()[0]} rows")
            st = status(cur)
            if st["new_periods"] or st["chain_errors"]:
                halt(f"after sync-new: {format_status(st, 'S1')}")
        if args.commit:
            conn.commit()
            print(f"COMMITTED: {len(done)} period(s) recorded as edition 1")
        else:
            conn.rollback()
            print("SIMULATION: ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


def cmd_refresh_latest(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    accept = tuple(args.accept_drift or ())
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            plan, drift = _plan(cur, accept)
            print("rows refresh-latest would write: "
                  + (", ".join(f"{p}={v['rows']} (edition {v['edition']}, "
                               f"{v['kind']})" for p, v in sorted(plan.items()))
                     or "none")
                  + f" (total {sum(v['rows'] for v in plan.values())})")
            if drift:
                halt(f"drift: live differs from the latest edition and matches "
                     f"no stored edition for {drift}; load it as an edition or "
                     "pass --accept-drift PERIOD to overwrite it")
            if _unrepairable(plan):
                halt(f"{_unrepairable(plan)} differ from the latest edition "
                     "in rows present in only one of them; an update cannot "
                     "repair that")
            if not writing:
                print("DRY RUN: nothing written (use --commit or --simulate)")
                return
            res = refresh_latest(cur, accept)
        if not res["rows"]:
            print("nothing to refresh")
        else:
            print(f"{res['rows']} live rows refreshed in "
                  f"{sorted(res['updated'])}; before/after guard and W1 "
                  "equivalence passed")
            if res["drift_accepted"]:
                print(f"drift overwritten by request: {res['drift_accepted']}")
            w1 = res["w1"]
            print(f"signals differ for {len(w1['authorities_differing'])} "
                  f"authorities in {sorted(w1['columns'])}")
            print("national before: " + json.dumps(w1["national_before"],
                                                    default=str))
            print("national after : " + json.dumps(w1["national_after"],
                                                    default=str))
            print("reproduction: " + ", ".join(
                f"{p} diff={d} reproduces={r}" for p, d, r in res["reproduction"]))
            if any(r is False for _, _, r in res["reproduction"]):
                halt(f"reproduction check failed for {res['reproduction']}")
        if args.commit:
            conn.commit()
            print("COMMITTED")
        else:
            conn.rollback()
            print("SIMULATION: ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# load-new: a brand-new quarter, loaded through the editions machinery
# ---------------------------------------------------------------------------

PROCEDURE = __doc__[__doc__.index("New-quarter procedure"):
                   __doc__.index("Helpers imported by later steps")]
LAYOUT_TITLE = "statutory homelessness: detailed local authority-level tables"
MONTH_NUMBER = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), 1)}
# first month of a financial-year quarter -> quarter number
QUARTER_OF_FIRST_MONTH = {4: 1, 7: 2, 10: 3, 1: 4}
RANGE_TEXT = re.compile(r"([A-Za-z]+)\s*(?:to|-|–)\s*([A-Za-z]+)\s+(\d{4})")
RELEASED_TEXT = re.compile(r"Released:\s*(\d{1,2} [A-Za-z]+ \d{4})")
COVER_TITLE = re.compile(r",\s*([A-Za-z]+ to [A-Za-z]+ \d{4}),\s*England\s*$")
DATE_FORMATS = ("%A, %B %d, %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d %B %Y")


def quarter_from_range(start: str, end: str, year: int) -> tuple:
    """(period, 'Mon-Mon YYYY') for a three-month range in one calendar year.

    Financial-year quarters: April-June is Q1, July-September Q2,
    October-December Q3 and January-March Q4 of the year that BEGAN the
    previous April (so 'January to March' of a year is Q4 of the year before).
    Anything that is not exactly one such quarter halts."""
    s, e = MONTH_NUMBER.get(start.lower()), MONTH_NUMBER.get(end.lower())
    if s is None or e is None or s not in QUARTER_OF_FIRST_MONTH \
            or e != (s + 1) % 12 + 1:
        halt(f"'{start} to {end} {year}' is not one financial-year quarter "
             "(April-June, July-September, October-December, January-March)")
    q = QUARTER_OF_FIRST_MONTH[s]
    return (f"{year - 1 if q == 4 else year}Q{q}",
            f"{start[:3].title()}-{end[:3].title()} {year}")


def _text_lines(rows) -> list:
    """One string per non-empty row: its non-empty cells, whitespace
    collapsed, joined with ' | ' (so a one-cell row is just its text)."""
    out = []
    for r in rows:
        cells = [" ".join(c.split()) for c in r if c and c.strip()]
        if cells:
            out.append(" | ".join(cells))
    return out


def _parse_release_date(text: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    halt(f"cover sheet release date {text!r} is in a format this reader does "
         "not know; not guessing")


def cover_sheet_info(path: Path) -> dict:
    """What the file's own front sheet says it is: {period, range_label,
    released (date), layout}. Two layouts exist among the files held:

    'cover'    (2025Q4 onwards): a sheet named Cover whose title row reads
               '...tables, <Month> to <Month> <year>, England' and a row
               'Released: <D Month YYYY>'.
    'contents' (2023Q2 to 2025Q3): no Cover sheet; the Contents sheet carries,
               one per row, the title 'Statutory homelessness: Detailed local
               authority-level tables', the period ('October to December
               2025' or 'July-September 2023'), 'England' and the date.
    Any other layout halts; nothing is guessed."""
    from s1_extract_ods import read_sheets
    path = Path(path)
    try:
        sheets = read_sheets(path, {"Cover", "Contents"},
                             stop_after="Contents")
    except SystemExit:
        raise
    except Exception as e:
        halt(f"{path.name}: cannot read the front sheets ({type(e).__name__})")
    if "Cover" in sheets:
        lines = _text_lines(sheets["Cover"])
        titles = [t for t in lines if t.lower().startswith(LAYOUT_TITLE)]
        if len(titles) != 1:
            halt(f"{path.name}: Cover sheet carries {len(titles)} title rows, "
                 "expected exactly 1")
        (title,) = titles
        m = COVER_TITLE.search(title)
        if LAYOUT_TITLE not in title.lower() or not m:
            halt(f"{path.name}: Cover title {title!r} is not "
                 "'Statutory homelessness: Detailed local authority-level "
                 "tables, <Month> to <Month> <year>, England'")
        rel = [m2.group(1) for t in lines
               for m2 in [RELEASED_TEXT.fullmatch(t)] if m2]
        if len(rel) != 1:
            halt(f"{path.name}: Cover sheet carries {len(rel)} 'Released:' "
                 "rows, expected exactly 1")
        (rel_text,) = rel
        rng, released, layout = (m.group(1), _parse_release_date(rel_text),
                                 "cover")
    elif "Contents" in sheets:
        lines = _text_lines(sheets["Contents"])
        at = [i for i, t in enumerate(lines) if t.lower() == LAYOUT_TITLE]
        if len(at) != 1:
            halt(f"{path.name}: no Cover sheet, and the Contents sheet has "
                 f"{len(at)} title rows '{LAYOUT_TITLE}', expected exactly 1")
        start = at.pop() + 1
        block = lines[start:start + 3]
        if len(block) != 3 or any(" | " in t for t in block):
            halt(f"{path.name}: the rows after the Contents title are not "
                 "period, 'England', date")
        period_text, country, date_text = block
        if country.lower() != "england":
            halt(f"{path.name}: the Contents title is followed by "
                 f"{country!r}, expected England")
        rng, released, layout = period_text, _parse_release_date(date_text), \
            "contents"
    else:
        halt(f"{path.name}: neither a Cover nor a Contents sheet found; the "
             "front-sheet layout is unknown")
    m = RANGE_TEXT.fullmatch(rng)
    if not m:
        halt(f"{path.name}: period text {rng!r} is not '<Month> to <Month> "
             "<year>'")
    period, label = quarter_from_range(m.group(1), m.group(2), int(m.group(3)))
    return {"period": period, "range_label": label, "released": released,
            "layout": layout}


def cover_sheet_period(path: Path) -> tuple:
    """(period, released_text) from the file's own front sheet; released_text
    is 'D Month YYYY'. Halts with a clear message if it cannot be read."""
    info = cover_sheet_info(path)
    d = info["released"]
    return info["period"], f"{d.day} {d.strftime('%B %Y')}"


def check_cover(path: Path, period: str, entry: dict) -> list:
    """Problems if the file's own front sheet disagrees with the period or with
    the manifest entry's release_label_actual / published_date_actual (both
    must be present). This refuses a file filed under the wrong release: a
    label typed into code or the manifest is never trusted without opening the
    file."""
    tag = entry.get("file", Path(path).name)
    try:
        found, released = cover_sheet_period(path)
    except SystemExit as e:
        return [f"{tag}: {e}"]
    bad = []
    if found != period:
        bad.append(f"{tag}: front sheet says {found}, not {period}")
    label = entry.get("release_label_actual")
    want = entry.get("published_date_actual")
    if not label or not want:
        return bad + [f"{tag}: manifest entry lacks release_label_actual / "
                      "published_date_actual"]
    if _parse_release_date(released).isoformat() != want:
        bad.append(f"{tag}: front sheet says released {released}, manifest "
                   f"published_date_actual is {want}")
    if released.lower() not in label.lower():
        bad.append(f"{tag}: release_label_actual {label!r} does not carry "
                   f"the front sheet's release date {released}")
    return bad


A1_MEASURES = ("total_assessments", "owed_duty", "prevention_duty",
               "relief_duty")
# the markers the publisher uses for a suppressed / not-available cell; this
# list is the A1 re-read's own and is not shared with the extractor
A1_MARKERS = frozenset(("", "-", "..", ":", "*", "x", "[x]", "[c]", "[z]",
                        "[low]", "n/a"))


def classify_a1_cell(cell) -> tuple:
    """('suppressed', None) for a marker or blank, ('number', n) for a numeric
    cell (rounded as the publisher's rounded figures are stored), else
    ('unrecognised', None). Deliberately independent of the extractor's num."""
    t = str(cell if cell is not None else "").strip().lower()
    if t in A1_MARKERS:
        return "suppressed", None
    try:
        return "number", int(round(float(t.replace(",", ""))))
    except ValueError:
        return "unrecognised", None


def raw_a1_cells(path) -> dict:
    """lad24cd -> {measure: published cell text} for the four A1 measures of
    one file. Shares only the column resolution (header text) and the code
    recode with the extractor; the cell text is kept as published."""
    from s1_extract_ods import (LA_CODE, COLUMN_LABELS, code_resolution,
                                read_sheets, resolve_columns)
    rows = read_sheets(path, {"A1"})["A1"]
    idx = resolve_columns(rows, COLUMN_LABELS["A1"], "A1")
    _, recode = code_resolution()
    out = {}
    for row in rows:
        code = (next(iter(row), "") or "").strip()
        if LA_CODE.fullmatch(code):
            out[recode.get(code, code)] = {
                m: (row[j] if j < len(row) else "") for m, j in idx.items()}
    return out


def check_a1_raw(cur, period, edition, raw=None) -> tuple:
    """The independent raw-cell gate for the four A1 measures
    (total_assessments, owed_duty, prevention_duty, relief_duty) of one
    edition: each stored value must equal its own classification of the
    published cell (marker -> NULL, number -> that number, so a stored 0 needs
    a published 0). raw ({lad: {measure: cell}}) defaults to a re-read of the
    edition's source file. -> (problems, rows, zeros)."""
    tag = f"{period} ed{edition}"
    if raw is None:
        path, files = edition_source_path(cur, period, edition)
        if path is None:
            return [f"{tag}: source file {files} not found in raw dir"], 0, 0
        raw = raw_a1_cells(path)
    cur.execute(f"""SELECT lad24cd, {', '.join(A1_MEASURES)} FROM
                    public.{TABLE} WHERE period = %s AND edition = %s""",
                (period, edition))
    bad, n, zeros = [], 0, 0
    for lad, *stored in cur.fetchall():
        n += 1
        cells = raw.get(lad, {})
        for m, val in zip(A1_MEASURES, stored):
            cell = cells.get(m)
            kind, want = classify_a1_cell(cell)
            if val == 0:
                zeros += 1
            if kind == "unrecognised":
                bad.append(f"{tag} {lad} {m}: unrecognised A1 cell {cell!r}")
            elif val != want:
                bad.append(f"{tag} {lad} {m}: stored {val}, A1 cell "
                           f"{cell!r}" + (" (suppressed)"
                                          if kind == "suppressed" else ""))
    return bad, n, zeros


def _resolve_expected(cur, given) -> int:
    """Authorities per quarter: the count derived from the live periods, or
    --expected-authorities where nothing can be derived (the same rule as
    sync-new: a given count that differs from a derivable one halts)."""
    derived = expected_authorities(cur)
    if given is not None and derived is not None and given != derived:
        halt(f"--expected-authorities {given} differs from the count "
             f"{derived} derived from the live periods; it is only accepted "
             "when nothing can be derived (or when it equals the derived "
             "count)")
    n = derived if derived is not None else given
    if n is None:
        halt("no live period to derive the authority count from; give "
             "--expected-authorities N")
    return n


def require_new_period(cur, period) -> None:
    """Halt unless the period has NO live rows and NO editions."""
    if not re.fullmatch(r"\d{4}Q[1-4]", period or ""):
        halt(f"{period!r} is not a period like 2026Q1")
    cur.execute(f"SELECT COUNT(*) FROM public.{LIVE} WHERE period = %s",
                (period,))
    live = cur.fetchone()[0]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} WHERE period = %s",
                (period,))
    eds = cur.fetchone()[0]
    if live or eds:
        halt(f"period already exists: {period} has {live} live rows and "
             f"{eds} edition rows. load-new is only for a quarter that is not "
             "stored; a revision of a stored quarter is loaded with `load`, "
             "and `status` shows what needs action")


def load_new(cur, period, recs, *, release_label, published_date, source_url,
             source_file, source_sha256, expected_authorities=None,
             ta1_cells=None, a3_totals=None, a1_cells=None,
             quarter_label=None) -> dict:
    """Record a brand-new quarter: file-backed edition 1, the live rows and
    the homelessness_quarter_urls row, then run the gates; halt on any
    failure (the caller rolls back, nothing is committed here).

    Refuses, before writing, a period with live rows or editions, no records,
    duplicate or unresolvable codes, a count other than the derived (or given)
    number of authorities, a non-integer measure, or a missing source_url.
    Stores NULL for a suppressed cell and 0 only for a published 0. Gates
    after the writes: edition 1 is the chain tip with the right row count and
    known codes; no suppressed TA1 cell is stored as 0 and every stored
    households_in_ta equals the re-read of the raw cell (ta1_cells defaults to
    the file's TA1 sheet, {lad: (cell, value)}); support_needs_total equals the
    independent A3 re-read (a3_totals defaults to the file's A3 sheet); the
    four A1 measures (total_assessments, owed_duty, prevention_duty,
    relief_duty) equal their own classification of the published A1 cells
    (a1_cells defaults to the file's A1 sheet, {lad: {measure: cell}}).
    Coverage of the independent raw re-reads: households_in_ta (TA1),
    support_needs_total (A3) and the four A1 measures, i.e. all six measures
    that are stored; the *_suspect columns are never written. The live rows equal edition 1 on every refresh column with the *_suspect
    columns NULL; status is clean. The quarter row is inserted if absent
    (needs quarter_label) or, if present, only marked loaded = true,
    loaded_at = now()."""
    require_new_period(cur, period)
    if not recs:
        halt(f"load-new {period}: no records supplied")
    codes = [r["lad24cd"] for r in recs]
    if len(set(codes)) != len(codes):
        halt(f"load-new {period}: duplicate authorities in the records")
    want = _resolve_expected(cur, expected_authorities)
    if len(recs) != want:
        halt(f"load-new {period}: {len(recs)} authorities supplied, expected "
             f"{want}")
    cur.execute("SELECT lad24cd FROM public.la_boundaries")
    known = {code for (code,) in cur.fetchall()}
    unknown = sorted(set(codes) - known)
    if unknown:
        halt(f"load-new {period}: {len(unknown)} code(s) do not resolve to "
             f"la_boundaries {unknown[:5]}")
    for r in recs:
        for m in LIVE_MEASURES:
            v = r.get(m)
            if v is not None and (not isinstance(v, int)
                                  or isinstance(v, bool) or v < 0):
                halt(f"load-new {period} {r['lad24cd']}: {m} is {v!r}, "
                     "expected a non-negative integer or NULL")
    if not source_url:
        halt(f"load-new {period}: source_url is required")
    cur.execute("SELECT file_url FROM public.homelessness_quarter_urls "
                "WHERE period = %s", (period,))
    urls_row = cur.fetchone()
    if urls_row is None and not quarter_label:
        halt(f"load-new {period}: no homelessness_quarter_urls row and no "
             "quarter_label to create one with")
    path = RAW_DIR / source_file
    if (ta1_cells is None or a3_totals is None or a1_cells is None) \
            and not path.exists():
        halt(f"load-new {period}: {path} not found, so the independent "
             "re-read of the raw cells cannot run")

    ed = _insert(cur, recs, period, release_label=release_label,
                 published_date=published_date, source_url=source_url,
                 source_file=source_file, source_sha256=source_sha256,
                 supersedes=None)
    cols = ", ".join(("lad24cd", "period") + LIVE_MEASURES
                     + ("source_file", "extracted_at"))
    cur.executemany(
        f"INSERT INTO public.{LIVE} ({cols}) VALUES "
        f"({', '.join(['%s'] * (len(LIVE_MEASURES) + 3))}, now())",
        [[r["lad24cd"], period] + [r.get(m) for m in LIVE_MEASURES]
         + [source_file] for r in recs])
    fmt = Path(source_file).suffix.lstrip(".").lower()
    cur.execute("""INSERT INTO public.homelessness_quarter_urls
                   (period, quarter_label, file_url, file_format, loaded,
                    loaded_at, notes)
                   VALUES (%s, %s, %s, %s, true, now(), %s)
                   ON CONFLICT (period) DO UPDATE
                   SET loaded = true, loaded_at = now()""",
                (period, quarter_label, source_url, fmt,
                 f"Loaded by s1_editions.py load-new from {source_file}; "
                 "edition 1."))

    problems = []
    if latest_edition(cur, period) != ed or ed != 1:
        problems.append(f"{period}: new edition {ed} is not edition 1 / the "
                        "chain tip")
    problems += check_registry(cur, period, ed, require_registry=False,
                               expected_rows=want)
    p7, rows, zeros = check_no_suppressed_zero(
        cur, period, ed, raw=ta1_cells if ta1_cells is not None
        else raw_ta1(path))
    problems += p7
    problems += check_support_needs(
        cur, period, ed, expected=a3_totals if a3_totals is not None
        else a3_expected(cur, path), expected_rows=want)
    p1, _, a1_zeros = check_a1_raw(
        cur, period, ed, raw=a1_cells if a1_cells is not None
        else raw_a1_cells(path))
    problems += p1
    live_diff = rows_differing(cur, LIVE, TABLE, ("lad24cd",), LIVE_MEASURES,
                               period, ed)
    if live_diff:
        problems.append(f"{period}: {live_diff} live rows differ from "
                        "edition 1")
    cur.execute(f"SELECT COUNT(*) FROM public.{LIVE} WHERE period = %s",
                (period,))
    n_live = cur.fetchone()[0]
    if n_live != want:
        problems.append(f"{period}: {n_live} live rows, expected {want}")
    suspect = " OR ".join(f"{c} IS NOT NULL" for c in LIVE_SUSPECT)
    cur.execute(f"SELECT COUNT(*) FROM public.{LIVE} WHERE period = %s "
                f"AND ({suspect})", (period,))
    if cur.fetchone()[0]:
        problems.append(f"{period}: a *_suspect column is not NULL")
    st = status(cur)
    if not st["ok"]:
        problems.append("status after the load: " + format_status(st, "S1"))
    cur.execute("SELECT loaded FROM public.homelessness_quarter_urls "
                "WHERE period = %s", (period,))
    if cur.fetchone() != (True,):
        problems.append(f"{period}: quarter row is not marked loaded")
    if problems:
        halt(f"load-new {period} failed its gates, rolled back: "
             + "; ".join(problems[:5]))
    cells = {m: sum(1 for r in recs if r.get(m) is None) for m in LIVE_MEASURES}
    return {"period": period, "edition": ed, "rows": len(recs),
            "live_rows": n_live, "null_cells": cells, "ta_zeros": zeros, "a1_zeros": a1_zeros,
            "quarter_row": "marked loaded" if urls_row else "inserted"}


MANIFEST_FIELDS = ("period", "file", "url", "sha256", "release_label",
                   "last_modified", "release_label_actual",
                   "published_date_actual")


def prepare_new_quarter(entry, period, expected) -> tuple:
    """Everything load-new needs from a manifest entry, without writing: the
    entry carries the actual-release fields; the file's sha256 matches; the
    file's own front sheet agrees with the period and the entry; the
    extraction has exactly `expected` authorities. -> (recs, path, cover)."""
    missing = [k for k in MANIFEST_FIELDS if not entry.get(k)]
    if missing:
        halt(f"manifest entry for {entry.get('file', '<no file>')} is missing "
             f"{missing}; a load-new entry needs {list(MANIFEST_FIELDS)} (see "
             "the load-new procedure in the module docstring)")
    try:
        date.fromisoformat(entry["published_date_actual"])
    except ValueError:
        halt(f"{entry['file']}: published_date_actual "
             f"{entry['published_date_actual']!r} is not an ISO yyyy-mm-dd date")
    if entry["period"] != period:
        halt(f"manifest entry is {entry['period']}, not {period}")
    path = RAW_DIR / entry["file"]
    if not path.exists():
        halt(f"{path} not found")
    if sha256_file(path) != entry["sha256"]:
        halt(f"{entry['file']}: sha256 does not match the manifest")
    problems = check_cover(path, period, entry)
    if problems:
        halt("front sheet does not match the period/manifest, refusing: "
             + "; ".join(problems))
    recs, path = _extract_entry(entry, expected)
    return recs, path, cover_sheet_info(path)


def cmd_load_new(args):
    manifest = load_manifest()
    if (args.manifest_entry is None) == (args.manifest_file is None):
        halt("give exactly one of --manifest-entry and --manifest-file")
    if args.manifest_file is not None:
        hits = [e for e in manifest if e["file"] == args.manifest_file]
        if len(hits) != 1:
            halt(f"{len(hits)} manifest entries for file "
                 f"{args.manifest_file!r}, expected exactly 1")
        (entry,) = hits
    else:
        if not 0 <= args.manifest_entry < len(manifest):
            halt(f"--manifest-entry {args.manifest_entry} outside "
                 f"0..{len(manifest) - 1}")
        entry = manifest[args.manifest_entry]
    from _db import get_conn
    conn = get_conn()  # the dry run runs the database gates too, then rolls back
    try:
        with conn.cursor() as cur:
            require_new_period(cur, args.period)
            want = _resolve_expected(cur, args.expected_authorities)
            recs, path, cover = prepare_new_quarter(entry, args.period, want)
            nulls = {m: sum(1 for r in recs if r.get(m) is None)
                     for m in LIVE_MEASURES}
            zeros = {m: sum(1 for r in recs if r.get(m) == 0)
                     for m in LIVE_MEASURES}
            print(f"load-new {args.period}: {entry['file']}\n"
                  f"  front sheet ({cover['layout']} layout): "
                  f"{cover['period']}, released {cover['released']}\n"
                  f"  {len(recs)} authorities (expected {want}); NULL cells "
                  f"{nulls}; real zeros {zeros}")
            res = load_new(
                cur, args.period, recs,
                release_label=entry["release_label_actual"],
                published_date=date.fromisoformat(
                    entry["published_date_actual"]),
                source_url=entry["url"], source_file=entry["file"],
                source_sha256=entry["sha256"],
                expected_authorities=args.expected_authorities,
                quarter_label=cover["range_label"])
            print(f"  edition {res['edition']}: {res['rows']} rows; live "
                  f"{res['live_rows']} rows; quarter row {res['quarter_row']}")
        if args.commit:
            conn.commit()
            print(f"COMMITTED: {args.period} loaded; gates passed in the same "
                  "transaction")
        else:
            conn.rollback()
            print("SIMULATION: gates passed, ROLLED BACK (nothing persisted)"
                  if args.simulate else
                  "DRY RUN: the database gates ran in a transaction that was "
                  "ROLLED BACK; nothing written (use --commit to record)")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddl", help="create the table and triggers if absent"
                   ).set_defaults(func=cmd_ddl)
    sub.add_parser("backfill", help="record edition 1 of stored quarters and "
                   "the 2025Q2 revision").set_defaults(func=cmd_backfill)
    d = sub.add_parser("diff", help="diff two stored editions of a period")
    d.add_argument("--period", required=True)
    d.add_argument("--new", type=int, required=True)
    d.add_argument("--old", type=int, required=True)
    d.set_defaults(func=cmd_diff)
    ld = sub.add_parser("load", help="extract a manifest file and diff it "
                        "(dry-run); insert only with --commit")
    ld.add_argument("--period", required=True)
    ld.add_argument("--manifest-label", help="select the period's manifest "
                    "entry by release_label, e.g. registry (preferred)")
    ld.add_argument("--manifest-entry", type=int,
                    help="alternative to --manifest-label: 0-BASED index into "
                    "s1_editions_manifest.json")
    ld.add_argument("--allow-non-registry", action="store_true",
                    help="permit loading a non-registry manifest file")
    ld.add_argument("--supersedes", type=int, help="must equal the current "
                    "chain tip of the period (default: the tip)")
    ld.add_argument("--published-date", help="YYYY-MM-DD; default manifest "
                    "last_modified date")
    mode = ld.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                    help="insert the edition (append-only, irreversible)")
    mode.add_argument("--simulate", action="store_true",
                    help="run the full --commit path (insert, gates 6-8) and "
                    "roll back instead of committing; persists nothing")
    ld.set_defaults(func=cmd_load)
    ln = sub.add_parser(
        "load-new", help="load a brand-new quarter from a manifest file "
        "(dry-run by default; the dry run also runs the database gates and "
        "rolls back)", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Load a quarter that is not stored yet.\n" + PROCEDURE)
    ln.add_argument("--period", required=True)
    ln.add_argument("--manifest-entry", type=int, help="0-BASED index into "
                    "s1_editions_manifest.json")
    ln.add_argument("--manifest-file", help="file name of the manifest entry")
    ln.add_argument("--expected-authorities", type=int, metavar="N",
                    help="only when no count can be derived from the live "
                    "periods (otherwise it must equal the derived count)")
    lmode = ln.add_mutually_exclusive_group()
    lmode.add_argument("--commit", action="store_true",
                       help="record the quarter (append-only, irreversible)")
    lmode.add_argument("--simulate", action="store_true",
                       help="run the full path and roll back")
    ln.set_defaults(func=cmd_load_new)
    rl = sub.add_parser("refresh-latest", help="copy latest editions into "
                        "la_statutory_homelessness (dry-run by default)")
    rmode = rl.add_mutually_exclusive_group()
    rmode.add_argument("--commit", action="store_true",
                       help="refresh the live table (before/after guard and "
                       "W1 equivalence in the same transaction)")
    rmode.add_argument("--simulate", action="store_true",
                       help="do everything, then roll back")
    rl.add_argument("--accept-drift", action="append", metavar="PERIOD",
                    help="overwrite this period although its live rows equal "
                    "no stored edition (repeatable)")
    rl.set_defaults(func=cmd_refresh_latest)
    sub.add_parser("status", help="what needs action; exit 1 if anything"
                   ).set_defaults(func=cmd_status)
    sn = sub.add_parser("sync-new", help="record live quarters with no "
                        "editions as edition 1 (dry-run by default)")
    sn.add_argument("--expected-authorities", type=int, metavar="N",
                    help="authorities per quarter; required only when no live "
                    "period has editions yet (otherwise derived)")
    smode = sn.add_mutually_exclusive_group()
    smode.add_argument("--commit", action="store_true")
    smode.add_argument("--simulate", action="store_true",
                       help="do everything, then roll back")
    sn.set_defaults(func=cmd_sync_new)
    da = sub.add_parser("dryrun-all", help="markdown dry-run report of every "
                        "manifest candidate; writes no table")
    da.add_argument("--out", required=True)
    da.set_defaults(func=cmd_dryrun_all)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
