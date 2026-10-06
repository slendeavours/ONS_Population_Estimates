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
    python scripts/s1_editions.py dryrun-all --out report.md
        # both manifest candidates of 2023Q2-2024Q4 vs stored edition 1 and
        # vs each other, as markdown; writes no table

Helpers imported by later steps: sha256_file, create_schema, insert_edition,
latest_edition, diff_editions, diff_records, and the table-parameterised
latest_map, rows_differing, classify_period, status, period_hashes,
guard_problems, modal_count.
"""
import argparse
import hashlib
import csv
import json
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_statutory_homelessness_editions"
TRIGGER = "s1_editions_immutable"
TRUNCATE_TRIGGER = "s1_editions_no_truncate"

MEASURES = ("total_assessments", "owed_duty", "prevention_duty", "relief_duty",
            "households_in_ta", "support_needs_total",
            "mental_health_suspect", "learning_disability_suspect",
            "drug_dependency_suspect", "alcohol_dependency_suspect",
            "rough_sleeping_history_suspect")


def halt(msg):
    sys.exit(f"HALT: {msg}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent."""
    measure_cols = ",\n        ".join(f"{m} integer" for m in MEASURES)
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS public.{TABLE} (
        lad24cd varchar(9) NOT NULL
            REFERENCES public.la_boundaries (lad24cd),
        period varchar(6) NOT NULL CHECK (period ~ '^\\d{{4}}Q[1-4]$'),
        edition integer NOT NULL,
        {measure_cols},
        release_label text,
        published_date date,
        source_url text,
        source_file text,
        source_sha256 text,
        loaded_at timestamptz DEFAULT now(),
        supersedes integer,
        PRIMARY KEY (lad24cd, period, edition)
    )""")
    cur.execute(f"""
    CREATE OR REPLACE FUNCTION public.{TRIGGER}() RETURNS trigger AS $f$
    BEGIN
        RAISE EXCEPTION '{TABLE} is append-only: % is not permitted', TG_OP;
    END
    $f$ LANGUAGE plpgsql""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {TRIGGER}
        BEFORE UPDATE OR DELETE ON public.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION public.{TRIGGER}()""")
    cur.execute(f"""CREATE OR REPLACE TRIGGER {TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON public.{TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION public.{TRIGGER}()""")


def insert_edition(cur, recs: list, period: str, *, release_label: str,
                   published_date: "date | None", source_url: "str | None",
                   source_file: str, source_sha256: str,
                   supersedes: "int | None") -> int:
    """Insert one edition of a period; return its edition number.

    If source_sha256 is already recorded for the period nothing is inserted and
    the existing edition number is returned. Missing measures are stored NULL.
    Empty recs is a hard stop: an edition with no rows cannot be recorded, and
    so is a `supersedes` that is not an existing edition of the period.
    """
    if not recs:
        halt(f"insert_edition: no records supplied for {period}")
    cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                "WHERE period = %s AND source_sha256 = %s",
                (period, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if existing:
        return existing[0]
    cur.execute(f"SELECT COALESCE(MAX(edition), 0) FROM public.{TABLE} "
                "WHERE period = %s", (period,))
    edition = cur.fetchone()[0] + 1
    if supersedes is not None:
        cur.execute(f"SELECT 1 FROM public.{TABLE} "
                    "WHERE period = %s AND edition = %s LIMIT 1",
                    (period, supersedes))
        if cur.fetchone() is None:
            halt(f"insert_edition: supersedes={supersedes} is not an existing "
                 f"edition of {period}")
    cols = (["lad24cd", "period", "edition"] + list(MEASURES) +
            ["release_label", "published_date", "source_url", "source_file",
             "source_sha256", "supersedes"])
    sql = (f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
           f"VALUES ({', '.join(['%s'] * len(cols))})")
    rows = []
    for r in recs:
        rows.append([r["lad24cd"], period, edition] +
                    [r.get(m) for m in MEASURES] +
                    [release_label, published_date, source_url, source_file,
                     source_sha256, supersedes])
    cur.executemany(sql, rows)
    return edition


EDITION_TABLES = (TABLE, "la_homelessness_support_needs_editions")


def latest_edition(cur, period: str, table: str = TABLE) -> int:
    """Tip of the period's supersedes chain.

    Ordering is the `supersedes` chain, not published_date (which is
    informational only). `table` must be one of EDITION_TABLES (the S1 and
    S1b editions tables). The whole structure is validated, and ValueError is
    raised unless it is one linear chain: exactly one root (supersedes NULL),
    every other edition supersedes a different existing edition, no edition is
    superseded twice (fork), and walking from the root reaches every edition
    (so a cycle or two editions superseding each other is refused). The tip is
    the end of that walk. LookupError if the period has no editions.
    """
    if table not in EDITION_TABLES:
        raise ValueError(f"latest_edition: unknown editions table {table!r}")
    cur.execute(f"SELECT DISTINCT edition, supersedes FROM public.{table} "
                "WHERE period = %s", (period,))
    rows = cur.fetchall()
    if not rows:
        raise LookupError(f"no editions recorded for {period}")
    sup = {}
    for ed, s in rows:
        if ed in sup:
            raise ValueError(f"{period}: edition {ed} has inconsistent "
                             "supersedes values across its rows")
        sup[ed] = s
    roots = sorted(e for e, s in sup.items() if s is None)
    if len(roots) != 1:
        raise ValueError(f"{period}: {len(roots)} editions {roots} have no "
                         "supersedes; the chain has no single root")
    for e, s in sorted(sup.items()):
        if s is not None and (s == e or s not in sup):
            raise ValueError(f"{period}: edition {e} supersedes "
                             f"{'itself' if s == e else f'missing edition {s}'}")
    children = {}
    for e, s in sup.items():
        if s is not None:
            children.setdefault(s, []).append(e)
    if any(len(c) > 1 for c in children.values()):
        tips = sorted(e for e in sup if e not in children)
        raise ValueError(f"{period}: supersedes chain has {len(tips)} tips "
                         f"{tips}; order is ambiguous (fork)")
    tip, seen = roots[0], {roots[0]}
    while tip in children:
        tip = children[tip][0]
        if tip in seen:
            break
        seen.add(tip)
    if seen != set(sup):
        raise ValueError(f"{period}: the chain from root {roots[0]} reaches "
                         f"{sorted(seen)} but editions {sorted(sup)} exist "
                         "(cycle or disconnected editions)")
    return tip


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
        ed = insert_edition(cur, recs, period, **kw)
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
# one-off dryrun-all report, gates 6-8 of the verify script and (until it is
# generalised) s1b_editions use it. The refresh path derives what to update
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
        "transition_totals": {t: totals.get(t, 0) for t in (
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
    path = RAW_DIR / files[0] if len(files) == 1 else None
    return (path if path is not None and path.exists() else None), files


def check_no_suppressed_zero(cur, period, edition, raw=None) -> tuple:
    """Gate 7 for one edition. raw (lad -> (cell, value)) defaults to a
    re-extraction of the edition's source file. -> (problems, rows, zeros)."""
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
        cell, val = raw.get(lad, (None, None))
        if stored == 0:
            zeros += 1
            try:
                published_zero = float(str(cell).replace(",", "")) == 0
            except ValueError:
                published_zero = False
            if not published_zero:
                bad.append(f"{tag} {lad}: stored 0, TA1 cell {cell!r}")
        if stored != val:
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
            ed = insert_edition(
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


LIVE = "la_statutory_homelessness"
LIVE_MEASURES = MEASURES[:6]
LIVE_SUSPECT = MEASURES[6:]
# columns refresh-latest may overwrite in the live table; nothing else changes
REFRESH_COLS = LIVE_MEASURES + ("source_file", "extracted_at")
W1_DIR = Path(__file__).resolve().parent / "verify" / "w1_live"
# signals columns that legitimately move when a revised households_in_ta
# reaches the current quarter or the prior-year quarter; data_quality is
# derived from them
TA_SIGNAL_COLS = {"ta_households_current", "ta_households_prev_year",
                  "ta_yoy_pct", "ta_trend_label", "data_quality"}
NATIONAL_TA_COLS = {"ta_households_current", "ta_households_prev_year",
                    "ta_yoy_pct"}


# ---------------------------------------------------------------------------
# Generic pieces (parameterised by table and column names so the S1b module
# can reuse them): chain tips, live-vs-edition comparison, status, the
# per-period before/after content hash.
# ---------------------------------------------------------------------------

def latest_map(cur, live=LIVE, editions=TABLE) -> tuple:
    """(tips, new_periods, chain_errors) for the periods present in `live`.

    tips: {period: chain-tip edition}; new_periods: periods with no editions;
    chain_errors: {period: message} where latest_edition raised ValueError.
    """
    cur.execute(f"SELECT DISTINCT period FROM public.{live} ORDER BY 1")
    tips, new, errors = {}, [], {}
    for (p,) in cur.fetchall():
        try:
            tips[p] = latest_edition(cur, p, table=editions)
        except LookupError:
            new.append(p)
        except ValueError as e:
            errors[p] = str(e)
    return tips, new, errors


def rows_differing(cur, live, editions, key_cols, cols, period, edition) -> int:
    """Rows of `period` that are in only one of the live table and the given
    edition, compared on key_cols + cols (NULL equals NULL)."""
    sel = ", ".join(tuple(key_cols) + tuple(cols))
    cur.execute(f"""SELECT
        (SELECT COUNT(*) FROM (SELECT {sel} FROM public.{live} WHERE period = %s
                               EXCEPT SELECT {sel} FROM public.{editions}
                               WHERE period = %s AND edition = %s) a)
      + (SELECT COUNT(*) FROM (SELECT {sel} FROM public.{editions}
                               WHERE period = %s AND edition = %s
                               EXCEPT SELECT {sel} FROM public.{live}
                               WHERE period = %s) b)""",
                (period, period, edition, period, edition, period))
    return cur.fetchone()[0]


def classify_period(cur, live, editions, key_cols, cols, period, tip) -> tuple:
    """('current'|'pending'|'drift', matched_edition).

    current: live equals the chain tip. pending: live differs from the tip but
    equals an earlier edition, i.e. a newer edition has been recorded and not
    yet refreshed. drift: live differs from the tip and equals no stored
    edition, so it was changed outside the editions machinery."""
    if rows_differing(cur, live, editions, key_cols, cols, period, tip) == 0:
        return "current", tip
    cur.execute(f"SELECT DISTINCT edition FROM public.{editions} "
                "WHERE period = %s AND edition <> %s ORDER BY 1 DESC",
                (period, tip))
    for (e,) in cur.fetchall():
        if rows_differing(cur, live, editions, key_cols, cols, period, e) == 0:
            return "pending", e
    return "drift", None


def live_period_counts(cur, live=LIVE) -> dict:
    cur.execute(f"SELECT period, COUNT(*) FROM public.{live} GROUP BY 1")
    return dict(cur.fetchall())


def modal_count(counts: dict) -> "int | None":
    """Most common per-period row count (the larger on a tie): the expected
    size of a period, derived from the data rather than typed in."""
    if not counts:
        return None
    c = Counter(counts.values())
    return max(c, key=lambda n: (c[n], n))


def expected_authorities(cur, live=LIVE) -> "int | None":
    return modal_count(live_period_counts(cur, live))


def status(cur, table_live=LIVE, table_editions=TABLE, key_cols=("lad24cd",),
           cols=LIVE_MEASURES, expected_rows=None) -> dict:
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
    """
    tips, new, errors = latest_map(cur, table_live, table_editions)
    drift, pending = [], []
    for p, tip in tips.items():
        kind, _ = classify_period(cur, table_live, table_editions, key_cols,
                                  cols, p, tip)
        if kind == "drift":
            drift.append(p)
        elif kind == "pending":
            pending.append(p)
    counts = live_period_counts(cur, table_live)
    mode = modal_count(counts)
    bad_counts = {}
    for p, n in counts.items():
        want = (expected_rows or {}).get(p, mode)
        if n != want:
            bad_counts[p] = (n, want)
    for p, tip in tips.items():
        cur.execute(f"SELECT COUNT(*) FROM public.{table_editions} "
                    "WHERE period = %s AND edition = %s", (p, tip))
        n = cur.fetchone()[0]
        if n != counts.get(p) and p not in bad_counts:
            bad_counts[p] = (counts.get(p), n)
    ok = not (new or drift or pending or errors or bad_counts)
    return {"new_periods": sorted(new), "drift_periods": sorted(drift),
            "pending_refresh": sorted(pending), "chain_errors": errors,
            "bad_counts": bad_counts, "periods": len(counts), "ok": ok}


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


def period_hashes(cur, table, key_cols, exclude=("loaded_at",)) -> dict:
    """{period: (row count, md5)} of each period's content, rows ordered by
    key_cols, every column except `exclude` (as jsonb text, so NULLs, types
    and column names all count)."""
    ex = "ARRAY[" + ", ".join(f"'{c}'" for c in exclude) + "]::text[]"
    order = ", ".join(f"t.{c}" for c in key_cols)
    cur.execute(f"""SELECT period, COUNT(*),
        md5(string_agg((to_jsonb(t) - {ex})::text, E'\\n' ORDER BY {order}))
        FROM public.{table} t GROUP BY 1""")
    return {p: (n, h) for p, n, h in cur.fetchall()}


def guard_problems(full_before, full_after, kept_before, kept_after,
                   intended) -> list:
    """The before/after rule of a refresh. full_*: period_hashes excluding
    loaded_at only. kept_*: period_hashes excluding the refresh columns (so
    loaded_at and every other column count). A period outside `intended`
    must be identical in full and in loaded_at; a period inside it may differ
    only in the refresh columns."""
    bad = []
    for p in sorted(set(full_before) | set(full_after)):
        if p in intended:
            continue
        if (full_before.get(p) != full_after.get(p)
                or kept_before.get(p) != kept_after.get(p)):
            bad.append(f"guard: {p} changed but was not being refreshed")
    for p in sorted(intended):
        if kept_before.get(p) != kept_after.get(p):
            bad.append(f"guard: {p} changed outside the refresh columns "
                       "(or its row count moved)")
    return bad


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
    return insert_edition(
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
    expected = expected_authorities_n or modal_count(known)
    if new and expected is None:
        halt("sync-new: no live period has editions, so the authority count "
             "cannot be derived; give --expected-authorities N")
    done = []
    for p in new:
        _record_as_loaded(cur, p, expected)
        done.append(p)
    return done


def _plan(cur, accept_drift=()) -> tuple:
    """({period: {edition, kind, rows}}, unaccepted drift periods)."""
    tips, new, errors = latest_map(cur)
    if new:
        halt(f"periods with no editions {new}; run sync-new first")
    if errors:
        halt("invalid edition chain: " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    plan, drift = {}, []
    for p, tip in tips.items():
        kind, _ = classify_period(cur, LIVE, TABLE, ("lad24cd",),
                                  LIVE_MEASURES, p, tip)
        if kind == "current":
            continue
        if kind == "drift" and p not in accept_drift:
            drift.append(p)
        plan[p] = {"edition": tip, "kind": kind}
    stray = sorted(set(accept_drift) - {p for p, v in plan.items()
                                        if v["kind"] == "drift"})
    if stray:
        halt(f"--accept-drift {stray}: not drifted periods, nothing to accept")
    diff = " OR ".join(f"l.{m} IS DISTINCT FROM e.{m}" for m in LIVE_MEASURES)
    for p, v in plan.items():
        cur.execute(f"""SELECT COUNT(*) FROM public.{LIVE} l
                        JOIN public.{TABLE} e ON e.lad24cd = l.lad24cd
                         AND e.period = l.period AND e.edition = %s
                        WHERE l.period = %s AND ({diff}
                          OR l.source_file IS DISTINCT FROM e.source_file)""",
                    (v["edition"], p))
        v["rows"] = cur.fetchone()[0]
    return plan, drift


def refresh_counts(cur, accept_drift=()) -> dict:
    """{period: rows refresh_latest would write} (read-only)."""
    plan, _ = _plan(cur, accept_drift)
    return {p: v["rows"] for p, v in sorted(plan.items())}


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
    """
    plan, drift = _plan(cur, accept_drift)
    if drift:
        halt("live differs from the latest edition and matches no stored "
             f"edition for {drift}: changed outside the editions tables. "
             "Load it as an edition, or re-run with --accept-drift PERIOD to "
             "overwrite it with the latest edition")
    result = {"updated": {}, "rows": 0,
              "drift_accepted": sorted(p for p, v in plan.items()
                                       if v["kind"] == "drift"),
              "w1": None, "reproduction": []}
    todo = {p: v for p, v in plan.items() if v["rows"]}
    if not todo:
        return result
    refreshed = set(todo)
    key = ("period", "lad24cd")
    full_b = period_hashes(cur, LIVE, key)
    kept_b = period_hashes(cur, LIVE, key, exclude=REFRESH_COLS)
    w1_b = w1_snapshot(cur)
    sets = ", ".join(f"{m} = e.{m}" for m in LIVE_MEASURES)
    diff = " OR ".join(f"l.{m} IS DISTINCT FROM e.{m}" for m in LIVE_MEASURES)
    pairs = tuple((p, v["edition"]) for p, v in sorted(todo.items()))
    cur.execute(f"""UPDATE public.{LIVE} l SET {sets},
                    source_file = e.source_file, extracted_at = e.loaded_at
                    FROM public.{TABLE} e
                    WHERE e.lad24cd = l.lad24cd AND e.period = l.period
                      AND (e.period, e.edition) IN %s
                      AND ({diff} OR l.source_file IS DISTINCT FROM e.source_file)""",
                (pairs,))
    n = cur.rowcount
    expected = sum(v["rows"] for v in todo.values())
    if _after_update_hook is not None:
        _after_update_hook(cur)
    full_a = period_hashes(cur, LIVE, key)
    kept_a = period_hashes(cur, LIVE, key, exclude=REFRESH_COLS)
    bad = guard_problems(full_b, full_a, kept_b, kept_a, refreshed)
    if n != expected:
        bad.append(f"wrote {n} rows, expected {expected}")
    for p, v in sorted(todo.items()):
        k = rows_differing(cur, LIVE, TABLE, ("lad24cd",), LIVE_MEASURES, p,
                           v["edition"])
        if k:
            bad.append(f"{p}: {k} rows still differ from edition "
                       f"{v['edition']} after the refresh")
    w1_bad, summary = check_w1_equivalence(w1_b, w1_snapshot(cur))
    bad += [f"W1: {x}" for x in w1_bad]
    if bad:
        halt("refresh-latest failed its before/after checks, rolled back: "
             + "; ".join(bad[:6]))
    result["updated"] = {p: v["rows"] for p, v in sorted(todo.items())}
    result["rows"] = n
    result["w1"] = summary
    result["reproduction"] = reproduction_update(cur, sorted(refreshed))
    return result


def reproduction_update(cur, periods) -> list:
    """Re-evaluate homelessness_quarter_urls for the periods just refreshed.

    'Reproduces from source' means: the live layer now equals the file named
    in the period's latest edition. The edition's file is re-extracted and
    compared cell by cell with the live six measures; reproduces_from_source is
    true only when 0 cells differ AND the row's file_url is the edition's
    source_url. reproduction_checked_at = now(); reproduction_diff_cells =
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
        repro = diff == 0 and file_url == url
        note = (f"Live layer re-extracted against {fname} (edition {ed}, "
                f"{'registry file' if repro else 'check failed'}): {diff} "
                "cells of the six S1 measures differ"
                + ("" if file_url == url else
                   f"; file_url differs from the edition's source_url ({url})")
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
