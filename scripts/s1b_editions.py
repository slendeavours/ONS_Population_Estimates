"""S1b edition history: append-only table of every published edition of each
quarter of the Table A3 support-needs data.

la_homelessness_support_needs holds one row per authority per category per
quarter (the latest layer). This module owns
la_homelessness_support_needs_editions, which keeps every published edition of
a quarter so a revision never overwrites what was sent. Rows are immutable:
triggers raise on UPDATE, DELETE and TRUNCATE. It mirrors s1_editions.py.
The editions machinery (schema, chain tip, status, refresh-latest and the
loader's inserts) is editions_core driven by SPEC; what is S1b's own (file
extraction, the gates, sync-new's category and source checks and its
'as loaded' hash) stays here.

Subcommands:
    python scripts/s1b_editions.py ddl                 # idempotent
    python scripts/s1b_editions.py backfill [--commit | --simulate]
        # edition 1 of the ten stored quarters other than 2025Q2 (the live rows
        # as loaded), plus 2025Q2 edition 1 (the ORIGINAL file) and edition 2
        # (the REVISED file, supersedes 1), both extracted from the local raw
        # files with the S1b reader. DRY-RUN by default (reports what would be
        # added, writes nothing). --commit inserts and runs the backfill gates
        # in the same transaction, rolling back on failure; --simulate does the
        # same and always rolls back. Idempotent: a second run adds nothing.
    python scripts/s1b_editions.py load --period P --manifest-label registry
        [--supersedes N] [--commit | --simulate]
        # DRY-RUN by default: extracts the manifest's registry file with the
        # S1b reader and diffs it against the period's latest stored edition;
        # writes nothing. --commit inserts it as the next edition and runs
        # gates 6-8 in the same transaction (rollback on failure); --simulate
        # does the same and always rolls back. --supersedes must equal the
        # chain tip.
    python scripts/s1b_editions.py dryrun-all --out report.md
        # plain-English diff of all seven registry files vs stored editions
    python scripts/s1b_editions.py status
        # what needs action (new quarter, newer edition not yet in live, live
        # changed outside the editions tables, bad row counts); exit 1 if any
    python scripts/s1b_editions.py sync-new [--expected-authorities N]
                                            [--accept-categories PERIOD]
                                            [--commit | --simulate]
        # records every live quarter that has no editions as edition 1 'as
        # loaded'; idempotent; DRY-RUN by default. Refuses a quarter whose
        # authority count or row count (authorities x categories) is not the
        # derived one, and a quarter whose category set differs from the
        # nearest earlier quarter's edition 1 unless --accept-categories names
        # it (a real layout change). --expected-authorities is only needed
        # (and only accepted) when no live quarter has editions yet.
    python scripts/s1b_editions.py refresh-latest [--commit | --simulate]
                                                  [--accept-drift PERIOD]
        # copies the latest edition into la_homelessness_support_needs for
        # every period whose live rows differ from it (value, value_flag,
        # category_label, source_url, source_edition, edition_variant only;
        # never loaded_at or any other column). DRY-RUN by default (prints
        # counts per period). --commit takes a per-period content hash before
        # and after in the same transaction (only the periods being refreshed
        # may change, and only in the refresh columns) and re-checks that each
        # refreshed period equals its edition; it rolls back on any failure.
        # --simulate does the same and always rolls back. A period whose live
        # rows equal no stored edition is drift (changed outside the editions
        # tables): the command halts unless --accept-drift PERIOD names it.

Helpers imported by later steps: create_schema, _insert, latest_edition,
check_coverage, check_latest_equals_live, check_q2_pair, status, sync_new,
refresh_latest.
"""
import argparse
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editions_core as core  # noqa: E402
from editions_core import modal_count  # noqa: E402
from s1_editions import (Q2_FILES, STALE_PERIODS, format_status,  # noqa: E402
                         halt, load_manifest, select_entry, sha256_file)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_homelessness_support_needs_editions"
LIVE = "la_homelessness_support_needs"
TRIGGER = "s1b_editions_immutable"
TRUNCATE_TRIGGER = "s1b_editions_no_truncate"

# every live column except loaded_at, in the live table's order
DATA_COLS = ("lad24cd", "period", "category_code", "value", "value_flag",
             "category_group", "category_label", "reference_quarter",
             "source_url", "source_edition", "edition_variant",
             "release_page_url", "layout_version", "publisher_la_code")
# the live columns refresh-latest may overwrite (and status compares)
REFRESH_COLS = ("value", "value_flag", "category_label", "source_url",
                "source_edition", "edition_variant")
KEY = ("lad24cd", "category_code")
HASH_KEY = ("period", "lad24cd", "category_code")

REPO = Path(__file__).resolve().parent.parent
RAW_DIR = REPO / "data" / "raw" / "s1b_a3"
REF_DIR = REPO / "data" / "reference"
BEFORE_CSV = REF_DIR / "statutory_homelessness_2025Q2_support_needs_before_revision.csv"
LIVE_LABEL = "as loaded; published_date is the load date"

# 2025Q2: (local raw file, S1 published date for that release, release_label,
# edition it supersedes). The dates are those s1_editions.Q2_FILES records for
# the same two publications.
Q2_ORIGINAL = "2025Q2_Statutory_Homelessness_Detailed_Local_Authority_Data_202509.ods"
Q2_REVISED = "2025Q2_Statutory_Homelessness_Detailed_Local_Authority_Data_202509_revised.ods"
Q2_SPECS = (
    (Q2_ORIGINAL, Q2_FILES[0][1], "Original", None),
    (Q2_REVISED, Q2_FILES[1][1], "Revised", 1),
)


def authority_counts(cur, table=LIVE) -> dict:
    """{period: distinct authorities} of a table."""
    cur.execute(f"SELECT period, COUNT(DISTINCT lad24cd) FROM public.{table} "
                "GROUP BY 1")
    return dict(cur.fetchall())


def expected_authorities(cur) -> "int | None":
    """Authorities per period, derived: the most common per-period count in
    the live table (None if the live table is empty)."""
    return modal_count(authority_counts(cur))


def edition1_category_counts(cur) -> dict:
    """{period: categories in the period's edition 1}."""
    cur.execute(f"""SELECT period, COUNT(DISTINCT category_code)
                    FROM public.{TABLE} WHERE edition = 1 GROUP BY 1""")
    return dict(cur.fetchall())


def expected_rows(cur, period) -> int:
    """Rows of one edition of a period: the derived authority count x the
    category count of the period's edition 1 (31 in the legacy layout, 32 in
    the 2025Q4 layout; read from the data, not typed)."""
    a = expected_authorities(cur)
    c = edition1_category_counts(cur).get(period)
    if a is None or c is None:
        raise LookupError(f"cannot derive the expected rows of {period}: "
                          "no live authority count or no edition 1")
    return a * c


def _live_expected_rows(cur, period) -> int:
    """As expected_rows, for a period whose edition 1 may not exist yet: the
    category count comes from the live rows."""
    cur.execute(f"SELECT COUNT(DISTINCT category_code) FROM public.{LIVE} "
                "WHERE period = %s", (period,))
    return expected_authorities(cur) * cur.fetchone()[0]


def _status_expected_rows(cur, period) -> int:
    """Live rows status expects of a period: the derived authority count x the
    period's category count (that of its edition 1, or the live rows' own for
    a period with no edition 1 yet). Exactly what S1b's status passed to
    s1_editions.status as expected_rows before the move to editions_core."""
    cur.execute(f"""SELECT COUNT(DISTINCT category_code) FROM public.{TABLE}
                    WHERE period = %s AND edition = 1""", (period,))
    cats = cur.fetchone()[0]
    if not cats:
        cur.execute(f"""SELECT COUNT(DISTINCT category_code)
                        FROM public.{LIVE} WHERE period = %s""", (period,))
        cats = cur.fetchone()[0]
    return expected_authorities(cur) * cats


_T = TABLE
# The S1b tables for editions_core. Every live column except loaded_at is a
# value or extra column (so insert_edition writes them all); compare_cols, the
# columns status and refresh-latest compare, are therefore exactly
# REFRESH_COLS, as before. Types and CHECKs are those of the real editions
# table. A fresh create_schema differs from the existing table only in column
# order and primary-key column order (accepted; the existing table is never
# re-created).
SPEC = core.EditionSpec(
    name="s1b",
    live_table=LIVE,
    editions_table=TABLE,
    key_cols=KEY,
    period_col="period",
    key_types=(("period", f"varchar(6) NOT NULL CONSTRAINT {_T}_period_chk "
                "CHECK (period ~ '^\\d{4}Q[1-4]$')"),),
    value_cols=(
        ("value", "integer"),
        ("value_flag", f"text CONSTRAINT {_T}_value_flag_chk CHECK "
         "(value_flag IS NULL OR value_flag IN "
         "('missing','suppressed','not_applicable'))"),
    ),
    extra_cols=(
        ("category_group", f"text NOT NULL CONSTRAINT {_T}_category_group_chk "
         "CHECK (category_group IN "
         "('support_need','needs_breakdown','needs_total','duty_total'))"),
        ("category_label", "text NOT NULL"),
        ("reference_quarter", "varchar(7) NOT NULL"),
        ("source_url", "text NOT NULL"),
        ("source_edition", "text NOT NULL"),
        ("edition_variant", f"text NOT NULL CONSTRAINT "
         f"{_T}_edition_variant_chk CHECK (edition_variant IN "
         "('original','revised','corrected','fixed'))"),
        ("release_page_url", "text NOT NULL"),
        ("layout_version", "text NOT NULL"),
        ("publisher_la_code", "varchar(9) NOT NULL"),
    ),
    table_constraints=(f"CONSTRAINT {_T}_value_xor_flag_chk "
                       "CHECK (num_nonnulls(value, value_flag) = 1)",),
    refresh_cols=REFRESH_COLS,
    expected_rows_per_period=_status_expected_rows,
    as_loaded_source_col="source_edition",
)


def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent
    (editions_core.create_schema with SPEC; the trigger function and triggers
    are the same names and text as before)."""
    core.create_schema(cur, SPEC)


def _insert(cur, rows: list, period: str, *, release_label: str,
            published_date, source_file: str, source_sha256: str,
            supersedes) -> int:
    """The only writer of editions (backfill, load, sync-new, and the verify
    gates): editions_core's
    insert_edition with SPEC, strict (a row lacking any column halts, as
    S1b's insert always refused one) and with supersedes required to be the
    period's chain tip (None only for a period with no editions)."""
    return core.insert_edition(cur, SPEC, rows, period,
                               release_label=release_label,
                               published_date=published_date,
                               source_file=source_file,
                               source_sha256=source_sha256,
                               supersedes=supersedes, strict=True)


def latest_edition(cur, period: str) -> int:
    """Chain tip of the period in the S1b editions table; the validation
    (single root, no fork, no self/dangling supersedes, whole chain reachable)
    is editions_core.chain_tip. Raises LookupError (no editions) or ValueError
    (broken chain) rather than halting, as callers here catch those."""
    return core.chain_tip(cur, SPEC, period)


def latest_map(cur) -> tuple:
    """(tips, new_periods, chain_errors) of the live periods
    (editions_core.latest_map with SPEC). A module-level name so that
    s1b_editions_verify gate 16 can stand in for it."""
    return core.latest_map(cur, SPEC)


# ---------------------------------------------------------------- checks

def check_coverage(cur) -> tuple:
    """Gate 4: every live period has edition 1; every edition has the derived
    authority count and that count x the period's edition-1 category count in
    rows. -> (problems, rows in edition 1, rows in table)."""
    cur.execute(f"SELECT DISTINCT period FROM public.{LIVE}")
    periods = sorted(r[0] for r in cur.fetchall())
    cur.execute(f"""SELECT period, edition, COUNT(DISTINCT lad24cd), COUNT(*)
                    FROM public.{TABLE} GROUP BY 1, 2""")
    eds = {}
    for p, e, n, rows in cur.fetchall():
        eds.setdefault(p, {})[e] = (n, rows)
    auths = expected_authorities(cur)
    cats = edition1_category_counts(cur)
    bad, n1 = [], 0
    for p in periods:
        e = eds.get(p, {})
        if 1 not in e:
            bad.append(f"{p}: no edition 1")
        n1 += e.get(1, (0, 0))[1]
    for p, e in sorted(eds.items()):
        if p not in cats:
            bad.append(f"{p}: editions {sorted(e)} but no edition 1")
            continue
        want = (auths, auths * cats[p])
        bad += [f"{p} ed{k}: {v[0]} authorities/{v[1]} rows, expected "
                f"{want[0]}/{want[1]}" for k, v in sorted(e.items())
                if v != want]
    cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
    return bad, n1, cur.fetchone()[0]


def _except_counts(cur, a_sql, a_args, b_sql, b_args) -> tuple:
    """(rows in A not in B, rows in B not in A); EXCEPT is NULL-safe."""
    out = []
    for x, xa, y, ya in ((a_sql, a_args, b_sql, b_args),
                         (b_sql, b_args, a_sql, a_args)):
        cur.execute(f"SELECT COUNT(*) FROM (({x}) EXCEPT ({y})) q", xa + ya)
        out.append(cur.fetchone()[0])
    return tuple(out)


def check_latest_equals_live(cur, periods=None) -> list:
    """Gate 5: for each live period the latest edition equals the live rows on
    every column except loaded_at, NULL-safe."""
    cur.execute(f"SELECT DISTINCT period FROM public.{LIVE}")
    live_periods = sorted(r[0] for r in cur.fetchall())
    bad = []
    for p in (periods or live_periods):
        cols = ", ".join(DATA_COLS)
        try:
            ed = latest_edition(cur, p)
        except (LookupError, ValueError) as e:
            bad.append(f"{p}: {e}")
            continue
        ed_only, live_only = _except_counts(
            cur,
            f"SELECT {cols} FROM public.{TABLE} WHERE period = %s "
            "AND edition = %s", (p, ed),
            f"SELECT {cols} FROM public.{LIVE} WHERE period = %s", (p,))
        if ed_only or live_only:
            bad.append(f"{p} ed{ed}: {ed_only} edition-only, {live_only} "
                       "live-only rows")
    return bad


def load_before_revision_csv() -> set:
    """The 2025Q2 before-revision CSV as a set of (lad24cd, category_code,
    value, value_flag); '' -> None."""
    out = set()
    with open(BEFORE_CSV, newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            v = (r["value"] or "").strip()
            f = (r["value_flag"] or "").strip()
            out.add((r["lad24cd"], r["category_code"],
                     int(float(v)) if v != "" else None, f or None))
    return out


def check_q2_pair(cur, csv_rows=None) -> list:
    """2025Q2 edition 1 equals the before-revision CSV on (lad24cd,
    category_code, value, value_flag), all 9,176 rows; edition 2 equals the
    live 2025Q2 rows (all columns bar loaded_at)."""
    csv_rows = load_before_revision_csv() if csv_rows is None else csv_rows
    cur.execute(f"""SELECT lad24cd, category_code, value, value_flag
                    FROM public.{TABLE} WHERE period = '2025Q2'
                      AND edition = 1""")
    ed1 = set(cur.fetchall())
    bad = []
    want = expected_rows(cur, "2025Q2")
    if len(csv_rows) != want:
        bad.append(f"CSV holds {len(csv_rows)} distinct rows, expected {want}")
    if len(ed1) != want:
        bad.append(f"2025Q2 edition 1 holds {len(ed1)} rows, expected {want}")
    if ed1 != csv_rows:
        bad.append(f"2025Q2 edition 1 vs CSV: {len(ed1 - csv_rows)} "
                   f"edition-only, {len(csv_rows - ed1)} CSV-only rows")
    cols = ", ".join(DATA_COLS)
    ed_only, live_only = _except_counts(
        cur, f"SELECT {cols} FROM public.{TABLE} WHERE period = '2025Q2' "
        "AND edition = 2", (),
        f"SELECT {cols} FROM public.{LIVE} WHERE period = '2025Q2'", ())
    if ed_only or live_only:
        bad.append(f"2025Q2 edition 2 vs live: {ed_only} edition-only, "
                   f"{live_only} live-only rows")
    return bad


# -------------------------------------------------------------- backfill

def live_rows_sha256(rows: list) -> str:
    """sha256 of a canonical rendering of one quarter's live rows: one line
    per row sorted by (lad24cd, category_code), the DATA_COLS joined by '|'
    with NULL as the empty string, lines joined by LF, UTF-8."""
    lines = []
    for r in sorted(rows, key=lambda r: (r["lad24cd"], r["category_code"])):
        lines.append("|".join("" if r[c] is None else str(r[c])
                              for c in DATA_COLS))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _count(cur, period, edition=None) -> int:
    q, a = f"SELECT COUNT(*) FROM public.{TABLE} WHERE period = %s", [period]
    if edition is not None:
        q, a = q + " AND edition = %s", a + [edition]
    cur.execute(q, a)
    return cur.fetchone()[0]


def _q2_edition(cur, path: Path, variant_url: str) -> dict:
    """The edition dict build_rows needs, for a local 2025Q2 raw file."""
    import s1b_support_needs_build as s1b
    cur.execute(f"""SELECT DISTINCT release_page_url, reference_quarter
                    FROM public.{LIVE} WHERE period = '2025Q2'""")
    (page, ref_q), = cur.fetchall()
    prefix = "2025Q2_"
    if not path.name.startswith(prefix):
        halt(f"{path.name}: local raw file name must start with {prefix}")
    filename = path.name[len(prefix):]
    return {"period": "2025Q2", "reference_quarter": ref_q,
            "url": variant_url, "filename": filename,
            "variant": s1b.edition_variant(filename),
            "release_page_url": page}


def _q2_urls(cur) -> dict:
    """{local file name: publisher URL}. Revised = the URL on the live rows;
    original = the register's file_url (the same asset the CSV era loaded)."""
    cur.execute(f"SELECT DISTINCT source_url FROM public.{LIVE} "
                "WHERE period = '2025Q2'")
    (revised,), = cur.fetchall()
    cur.execute("SELECT file_url FROM public.homelessness_quarter_urls "
                "WHERE period = '2025Q2'")
    (original,), = cur.fetchall()
    return {Q2_ORIGINAL: original, Q2_REVISED: revised}


def backfill(cur, plan_only=False) -> list:
    """Edition 1 for the ten live quarters other than 2025Q2, editions 1-2 for
    2025Q2. Live quarters are skipped when any edition already exists (the live
    layer may since have been refreshed, and re-reading it would record a
    spurious 'as loaded' edition). Returns [(period, edition, rows, action)].
    action is 'inserted', 'present' or (plan_only) 'would insert'."""
    import s1b_support_needs_build as s1b
    out = []
    cols = ", ".join(DATA_COLS)
    cur.execute(f"SELECT DISTINCT period FROM public.{LIVE} ORDER BY 1")
    periods = [r[0] for r in cur.fetchall()]
    exists = {}
    if plan_only:
        cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
        have_table = cur.fetchone()[0] is not None
    for period in periods:
        if period == "2025Q2":
            continue
        if plan_only and not have_table:
            n = 0
        else:
            n = _count(cur, period)
        if n:
            out.append((period, 1, n, "present"))
            continue
        cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date
                        FROM public.{LIVE} WHERE period = %s""", (period,))
        fetched = cur.fetchall()
        rows = [dict(zip(DATA_COLS, r[:-1])) for r in fetched]
        loaded = {r[-1] for r in fetched}
        files = {r["source_edition"] for r in rows}
        urls = {r["source_url"] for r in rows}
        if len(loaded) != 1 or len(files) != 1 or len(urls) != 1:
            halt(f"{period}: live rows differ in loaded_at/source_edition/"
                 f"source_url ({len(loaded)}/{len(files)}/{len(urls)})")
        if plan_only:
            out.append((period, 1, len(rows), "would insert"))
            continue
        ed = _insert(cur, rows, period, release_label=LIVE_LABEL,
                     published_date=loaded.pop(), source_file=files.pop(),
                     source_sha256=live_rows_sha256(rows), supersedes=None)
        out.append((period, ed, _count(cur, period, ed), "inserted"))

    urls = _q2_urls(cur)
    prev = None
    for fname, published, label, sup in Q2_SPECS:
        path = RAW_DIR / fname
        if not path.exists():
            halt(f"{path} not found")
        sha = sha256_file(path)
        cur.execute("SELECT to_regclass(%s)", (f"public.{TABLE}",))
        have = []
        if cur.fetchone()[0] is not None:
            cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                        "WHERE period = '2025Q2' AND source_sha256 = %s",
                        (sha,))
            have = [r[0] for r in cur.fetchall()]
        if have:
            prev = have[0]
            out.append(("2025Q2", prev, _count(cur, "2025Q2", prev), "present"))
            continue
        if plan_only:
            out.append(("2025Q2", None, _live_expected_rows(cur, "2025Q2"),
                        "would insert"))
            continue
        edition = _q2_edition(cur, path, urls[fname])
        df = s1b.read_a3(path)
        _, _, tuples = s1b.build_rows(cur, edition, df)
        rows = [dict(zip(DATA_COLS, t)) for t in tuples]
        want = _live_expected_rows(cur, "2025Q2")
        if len(rows) != want:
            halt(f"{fname}: {len(rows)} rows extracted, expected {want}")
        # Original: None (2025Q2 has no editions yet); Revised: prev, the
        # Original's edition, which is then the chain tip
        ed = _insert(cur, rows, "2025Q2", release_label=label,
                     published_date=published,
                     source_file=edition["filename"], source_sha256=sha,
                     supersedes=prev if sup is not None else None)
        prev = ed
        out.append(("2025Q2", ed, _count(cur, "2025Q2", ed), "inserted"))
    return out


def run_backfill_gates(cur) -> None:
    """Gates 4-5 plus the 2025Q2 pair; halt on any problem. Row totals are
    derived (authorities x categories per edition, in check_coverage), not
    typed."""
    bad, _, _ = check_coverage(cur)
    bad += check_q2_pair(cur)
    bad += check_latest_equals_live(cur)
    if bad:
        halt("backfill failed its gates, rolled back: " + "; ".join(bad[:6]))


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
    print(f"ddl: {TABLE} and triggers {TRIGGER}, {TRUNCATE_TRIGGER} present")


def cmd_backfill(args):
    from _db import get_conn, get_readonly_conn
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if writing:
                create_schema(cur)
            results = backfill(cur, plan_only=not writing)
            if writing:
                run_backfill_gates(cur)
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE}")
                total = cur.fetchone()[0]
        for period, ed, n, action in results:
            print(f"  {period} edition {ed}: {action} ({n} rows)")
        added = sum(r[3] in ("inserted", "would insert") for r in results)
        if not writing:
            print(f"DRY RUN: {added} edition(s) would be added; nothing "
                  "written (use --commit or --simulate)")
            return
        if args.commit:
            conn.commit()
            print(f"backfill: {added} edition(s) added; gates passed; "
                  f"{TABLE} now holds {total} rows. COMMITTED")
        else:
            conn.rollback()
            print(f"SIMULATION: {added} edition(s) added, gates passed, "
                  "ROLLED BACK (nothing persisted)")
    except BaseException:
        if writing:
            conn.rollback()
        raise
    finally:
        conn.close()


# ------------------------------------------------------------ load (Task 3)

ONE_OR_MORE = "hh_one_or_more_support_needs"
# The publisher's markers and the flag each is stored as: the same mapping as
# the extractor's s1b_support_needs_build.FLAGS (legacy '..' and '-', 2026
# layout '[x]', '[c]', '[z]'). A blank cell is not a marker; it is 'missing'
# by S1b's own rule (see _expected_from_raw).
MARKERS = {"..": "missing", "-": "suppressed", "[x]": "missing",
           "[c]": "suppressed", "[z]": "not_applicable"}


def raw_path(period, source_file):
    """Local raw file for a stored source_file (with or without the period
    prefix the raw folder uses). None if absent."""
    for cand in (RAW_DIR / source_file, RAW_DIR / f"{period}_{source_file}"):
        if cand.exists():
            return cand
    return None


def _edition1_facts(cur, period) -> dict:
    cur.execute(f"""SELECT DISTINCT release_page_url, reference_quarter,
                           layout_version FROM public.{TABLE}
                    WHERE period = %s AND edition = 1""", (period,))
    rows = cur.fetchall()
    if len(rows) != 1:
        halt(f"{period}: edition 1 has {len(rows)} release_page_url/"
             "reference_quarter/layout_version combinations, expected 1")
    return dict(zip(("release_page_url", "reference_quarter",
                     "layout_version"), rows[0]))


def extract_entry(cur, entry: dict) -> tuple:
    """Extract one manifest file with the S1b reader (verifying its sha256).
    -> (rows as dicts keyed by DATA_COLS, edition dict, local path, layout).
    The file is stored as a revised edition whatever its name says."""
    import s1b_support_needs_build as s1b
    period = entry["period"]
    path = RAW_DIR / entry["file"]
    if not path.exists():
        halt(f"{path} not found")
    sha = sha256_file(path)
    if sha != entry["sha256"]:
        halt(f"{entry['file']}: sha256 {sha} does not match manifest "
             f"{entry['sha256']}")
    prefix = f"{period}_"
    if not entry["file"].startswith(prefix):
        halt(f"{entry['file']}: manifest file name must start with {prefix}")
    facts = _edition1_facts(cur, period)
    edition = {"period": period, "reference_quarter": facts["reference_quarter"],
               "url": entry["url"], "filename": entry["file"][len(prefix):],
               "variant": "revised",
               "release_page_url": facts["release_page_url"]}
    layout, _, tuples = s1b.build_rows(cur, edition, s1b.read_a3(path))
    rows = [dict(zip(DATA_COLS, t)) for t in tuples]
    return rows, edition, path, layout


def diff_rows(cur, period, rows, old_edition) -> dict:
    """Cell diff of extracted rows against a stored edition (read-only)."""
    from collections import Counter
    cur.execute(f"""SELECT lad24cd, category_code, category_group, value,
                           value_flag FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, old_edition))
    old = {(r[0], r[1]): (r[2], r[3], r[4]) for r in cur.fetchall()}
    new = {(r["lad24cd"], r["category_code"]):
           (r["category_group"], r["value"], r["value_flag"]) for r in rows}
    kinds, by_group, by_cat, auth = Counter(), Counter(), Counter(), set()
    for k in set(old) & set(new):
        og, ov, of = old[k]
        _, nv, nf = new[k]
        if (ov, of) == (nv, nf):
            continue
        if ov is not None and nv is not None:
            kind = "value -> value"
        elif ov is not None:
            kind = "value -> flag"
        elif nv is not None:
            kind = "flag -> value"
        else:
            kind = "flag -> different flag"
        kinds[kind] += 1
        if ov == 0 and nv is None:
            kinds["  of which zero -> flag"] += 1
        if of is not None and nv == 0:
            kinds["  of which flag -> zero"] += 1
        by_group[og] += 1
        by_cat[k[1]] += 1
        auth.add(k[0])
    return {"kinds": kinds, "by_group": by_group, "by_category": by_cat,
            "authorities": auth, "only_new": sorted(set(new) - set(old)),
            "only_old": sorted(set(old) - set(new)),
            "changed": sum(by_group.values()),
            "compared": len(set(old) & set(new))}


def format_diff(d) -> str:
    kinds = ", ".join(f"{k}={v}" for k, v in sorted(d["kinds"].items())
                      if not k.startswith(" ")) or "none"
    return (f"{d['changed']} of {d['compared']} cells changed in "
            f"{len(d['authorities'])} authorities; {kinds}"
            + (f"; only_in_new={len(d['only_new'])}" if d["only_new"] else "")
            + (f"; only_in_old={len(d['only_old'])}" if d["only_old"] else ""))


def check_loaded(cur, period, edition, known_codes=None,
                 require_registry=True) -> list:
    """Gate 6 for one edition: the derived authority count and that count x the
    period's edition-1 category count in rows, every lad24cd inside la_code_lookup, a source_url, the same category set as
    edition 1, and (require_registry) label registry and variant revised."""
    cur.execute(f"""SELECT lad24cd, category_code, release_label, source_url,
                           edition_variant FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    rows = cur.fetchall()
    if known_codes is None:
        cur.execute("SELECT new_code FROM public.la_code_lookup UNION "
                    "SELECT old_code FROM public.la_code_lookup")
        known_codes = {r[0] for r in cur.fetchall()}
    tag = f"{period} ed{edition}"
    bad = []
    n = len({r[0] for r in rows})
    want = (expected_authorities(cur), expected_rows(cur, period))
    if (n, len(rows)) != want:
        bad.append(f"{tag}: {n} authorities/{len(rows)} rows, expected "
                   f"{want[0]}/{want[1]}")
    orphans = sorted({r[0] for r in rows} - set(known_codes))
    if orphans:
        bad.append(f"{tag}: {len(orphans)} lad24cd outside la_code_lookup "
                   f"{orphans[:3]}")
    if any(r[3] is None for r in rows):
        bad.append(f"{tag}: source_url missing")
    if edition != 1:
        cur.execute(f"""SELECT DISTINCT category_code FROM public.{TABLE}
                        WHERE period = %s AND edition = 1""", (period,))
        c1 = {r[0] for r in cur.fetchall()}
        cn = {r[1] for r in rows}
        if cn != c1:
            bad.append(f"{tag}: category set differs from edition 1 "
                       f"(only here {sorted(cn - c1)[:3]}, only ed1 "
                       f"{sorted(c1 - cn)[:3]}); {len(cn)} categories")
    if require_registry:
        if {r[2] for r in rows} != {"registry"}:
            bad.append(f"{tag}: release_label "
                       f"{sorted({str(r[2]) for r in rows})}, expected registry")
        if {r[4] for r in rows} != {"revised"}:
            bad.append(f"{tag}: edition_variant "
                       f"{sorted({r[4] for r in rows})}, expected revised")
    return bad


def raw_cells(cur, path) -> dict:
    """(lad24cd, category_code) -> the raw A3 cell of the source file, read
    straight from the sheet (only the column mapping and publisher-code
    resolution come from the S1b reader)."""
    import s1b_support_needs_build as s1b
    df = s1b.read_a3(path)
    _, mapping, _ = s1b.map_columns(df)
    rows = s1b.la_rows(df)
    resolved, unresolved = s1b.resolve_lookup(cur, rows)
    if unresolved:
        halt(f"{path.name}: unresolved publisher codes {unresolved}")
    out = {}
    for code, i in rows.items():
        for j, cat in mapping.items():
            out[(resolved[code], cat)] = df.iat[i, j]
    return out


def _expected_from_raw(raw):
    """(value, flag) a raw cell must be stored as, independent of s1b.cell():
    None/blank -> missing; a documented marker -> its flag; else the number.

    Not routed through blank_reader.read_cell. On the 174,640 mapped cells of
    the stored raw files the two agree on every cell (checked 2026-10-07,
    task 5), but they differ on inputs those files happen not to contain:
    read_cell refuses a blank or NaN cell, which S1b's rule (and its
    extractor, s1b_support_needs_build.cell) stores as 'missing', and it
    accepts '1,234', which S1b refuses. Gate 7 would change, so it stays."""
    if raw is None or (isinstance(raw, float) and raw != raw):
        return None, "missing"
    text = str(raw).strip()
    if text == "":
        return None, "missing"
    if text in MARKERS:
        return None, MARKERS[text]
    return int(round(float(text))), None


def check_flag_integrity(cur, period, edition, raw=None) -> tuple:
    """Gate 7 for one edition: every stored cell equals an independent re-read
    of its raw cell - in particular no 0 is stored where the source cell is a
    suppression marker or blank, and no marker is stored as a number. raw
    ((lad, category) -> cell) defaults to the edition's source file.
    -> (problems, rows checked, stored zeros)."""
    tag = f"{period} ed{edition}"
    cur.execute(f"""SELECT lad24cd, category_code, value, value_flag,
                           source_file FROM public.{TABLE}
                    WHERE period = %s AND edition = %s""", (period, edition))
    stored = cur.fetchall()
    if raw is None:
        files = {r[4] for r in stored}
        path = raw_path(period, next(iter(files))) if len(files) == 1 else None
        if path is None:
            return [f"{tag}: source file {sorted(files)} not found in raw "
                    "dir"], 0, 0
        raw = raw_cells(cur, path)
    bad, zeros = [], 0
    for lad, cat, v, f, _ in stored:
        if v == 0:
            zeros += 1
        if (lad, cat) not in raw:
            bad.append(f"{tag} {lad} {cat}: no raw cell")
            continue
        try:
            ev, ef = _expected_from_raw(raw[(lad, cat)])
        except ValueError:
            bad.append(f"{tag} {lad} {cat}: raw cell {raw[(lad, cat)]!r} is "
                       "neither a number nor a marker")
            continue
        if (v, f) != (ev, ef):
            bad.append(f"{tag} {lad} {cat}: stored ({v}, {f}), raw cell "
                       f"{raw[(lad, cat)]!r} -> ({ev}, {ef})")
    want = expected_rows(cur, period)
    if len(stored) != want:
        bad.append(f"{tag}: {len(stored)} stored cells, expected {want}")
    return bad, len(stored), zeros


def check_s1_cross(cur, period, edition, s1=None) -> list:
    """Gate 8 for one edition: the 'one or more support needs' value of each
    authority equals la_statutory_homelessness.support_needs_total for the same
    lad24cd and period, NULL-safe, for every authority; a flagged S1b cell against a
    non-NULL S1 value (or the reverse) is called out."""
    tag = f"{period} ed{edition}"
    if s1 is None:
        cur.execute("""SELECT lad24cd, support_needs_total FROM
                       public.la_statutory_homelessness WHERE period = %s""",
                    (period,))
        s1 = dict(cur.fetchall())
    cur.execute(f"""SELECT lad24cd, value, value_flag FROM public.{TABLE}
                    WHERE period = %s AND edition = %s AND category_code = %s""",
                (period, edition, ONE_OR_MORE))
    got = cur.fetchall()
    bad, n = [], 0
    for lad, v, f in got:
        if lad not in s1:
            bad.append(f"{lad}: not in la_statutory_homelessness")
        elif s1[lad] == v:
            n += 1
        elif f is not None and s1[lad] is not None:
            bad.append(f"{lad}: S1b flagged {f}, S1 value {s1[lad]}")
        elif f is None and s1[lad] is None:
            bad.append(f"{lad}: S1b value {v}, S1 NULL")
        else:
            bad.append(f"{lad}: S1b {v}, S1 {s1[lad]}")
    auths = expected_authorities(cur)
    if n != auths or len(got) != auths:
        return [f"{tag}: {n}/{auths} equal ({len(got)} stored rows)"
                + (f"; e.g. {bad[:3]}" if bad else "")]
    return []


def run_load_gates(cur, period, edition) -> None:
    """Gates 6-8 for the edition just inserted; halt on any problem."""
    problems = check_loaded(cur, period, edition)
    p7, _, _ = check_flag_integrity(cur, period, edition)
    problems += p7 + check_s1_cross(cur, period, edition)
    if problems:
        halt(f"{period} edition {edition} failed gates 6-8, rolled back: "
             + "; ".join(problems[:5]))


def cmd_load(args):
    from datetime import date
    from _db import get_conn, get_readonly_conn
    entry = select_entry(load_manifest(), args.period,
                         args.manifest_label, args.manifest_entry)
    if entry["release_label"] != "registry":
        halt(f"{entry['file']} has release_label {entry['release_label']!r}; "
             "only registry files load as editions")
    published = date.fromisoformat(
        args.published_date or entry["last_modified"][:10])
    writing = args.commit or args.simulate
    conn = get_conn() if writing else get_readonly_conn()
    try:
        with conn.cursor() as cur:
            if not (RAW_DIR / entry["file"]).exists():
                halt(f"{RAW_DIR / entry['file']} not found")
            sha = sha256_file(RAW_DIR / entry["file"])
            cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                        "WHERE period = %s AND source_sha256 = %s",
                        (args.period, sha))
            have = [r[0] for r in cur.fetchall()]
            if have:
                print(f"already loaded (edition {have[0]}), nothing inserted")
                conn.rollback()
                return
            try:
                prev = latest_edition(cur, args.period)
            except (LookupError, ValueError) as e:
                halt(f"cannot determine latest edition: {e}")
            if args.supersedes is not None and args.supersedes != prev:
                halt(f"--supersedes {args.supersedes} is not the current "
                     f"chain tip (edition {prev}) of {args.period}")
            rows, edition, path, layout = extract_entry(cur, entry)
            d = diff_rows(cur, args.period, rows, prev)
            print(f"{args.period} {entry['file']} (published {published}, "
                  f"layout {layout}) vs stored edition {prev}:\n  "
                  f"{format_diff(d)}")
            if not d["changed"] and not d["only_new"] and not d["only_old"]:
                print("  NOTE: identical in values to the previous edition; "
                      "it would be recorded as a no_change edition")
            if not writing:
                print("DRY RUN: nothing written (use --commit to insert)")
                return
            ed = _insert(  # prev is the chain tip (latest_edition above)
                cur, rows, args.period, release_label="registry",
                published_date=published,
                source_file=edition["filename"], source_sha256=sha,
                supersedes=prev)
            if latest_edition(cur, args.period) != ed:
                halt(f"{args.period}: new edition {ed} is not the chain tip")
            run_load_gates(cur, args.period, ed)
        if args.commit:
            conn.commit()
            print(f"loaded {args.period} edition {ed} "
                  f"({len(rows)} rows); gates 6-8 passed in "
                  "the same transaction")
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


def cmd_dryrun_all(args):
    from _db import get_readonly_conn
    manifest = [e for e in load_manifest() if e["release_label"] == "registry"
                and e["period"] in STALE_PERIODS]
    if len(manifest) != 7:
        halt(f"{len(manifest)} registry entries for the seven quarters")
    out = ["# Task 3 dry-run: registry files vs latest stored edition", "",
           "Generated by `s1b_editions.py dryrun-all`. Nothing written to any "
           "table. Each registry file is extracted with the S1b reader and "
           "compared cell by cell (authority x category) with the period's "
           "latest stored edition. A cell is either a number or a flag "
           "(missing / suppressed / not applicable); a flag is stored as NULL "
           "with a reason, never as 0.", ""]
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for e in sorted(manifest, key=lambda x: x["period"]):
                p = e["period"]
                prev = latest_edition(cur, p)
                out += [f"## {p}", "",
                        f"File `{e['file']}`, published {e['last_modified'][:10]}"
                        f"; compared with stored edition {prev}.", ""]
                try:
                    rows, _, _, layout = extract_entry(cur, e)
                except SystemExit as x:
                    out += [f"- EXTRACTION WARNING: {x}", ""]
                    continue
                facts = _edition1_facts(cur, p)
                d = diff_rows(cur, p, rows, prev)
                cats = {r["category_code"] for r in rows}
                auths = {r["lad24cd"] for r in rows}
                cur.execute(f"""SELECT DISTINCT category_code FROM
                                public.{TABLE} WHERE period = %s
                                AND edition = %s""", (p, prev))
                same_cats = cats == {r[0] for r in cur.fetchall()}
                cur.execute("""SELECT lad24cd, support_needs_total FROM
                               public.la_statutory_homelessness
                               WHERE period = %s""", (p,))
                s1 = dict(cur.fetchall())
                eq = sum(1 for r in rows if r["category_code"] == ONE_OR_MORE
                         and s1.get(r["lad24cd"]) == r["value"])
                same_layout = layout == facts["layout_version"]
                out += [f"- structure: {len(auths)} authorities, {len(cats)} "
                        f"categories, {len(rows)} rows (expected 296, "
                        f"{expected_rows(cur, p) // 296}, {expected_rows(cur, p)}); "
                        f"layout {layout} ("
                        f"{'same as' if same_layout else 'DIFFERENT from'} "
                        f"edition 1: {facts['layout_version']}); category set "
                        f"{'identical to' if same_cats else 'DIFFERENT from'} "
                        "the stored edition",
                        f"- cells compared: {d['compared']}; changed: "
                        f"{d['changed']} in {len(d['authorities'])} "
                        "authorities"]
                sub = {"value -> flag": "  of which zero -> flag",
                       "flag -> value": "  of which flag -> zero"}
                for k in ("value -> value", "value -> flag", "flag -> value",
                          "flag -> different flag"):
                    if d["kinds"].get(k):
                        out.append(f"  - {k}: {d['kinds'][k]}")
                        if d["kinds"].get(sub.get(k)):
                            out.append(f"    - {sub[k].strip()}: "
                                       f"{d['kinds'][sub[k]]}")
                out.append("- changed cells by category group: "
                           + (", ".join(f"{g}={n}" for g, n in
                                        sorted(d["by_group"].items()))
                              or "none"))
                top = d["by_category"].most_common(5)
                out.append("- most-changed categories: "
                           + (", ".join(f"{c}={n}" for c, n in top)
                              or "none"))
                out.append("- cells only in the new file: "
                           f"{len(d['only_new'])}; only in the stored edition: "
                           f"{len(d['only_old'])}")
                out += ["- S1 cross-check (gate 8 feed): 'one or more support "
                        "needs' equals la_statutory_homelessness."
                        f"support_needs_total for {eq}/296 authorities", ""]
    finally:
        conn.close()
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {path}")


# --------------------------------------------- status, sync-new, refresh-latest
#
# Generic for any quarter: nothing below names a period, a snapshot table or a
# row total. What needs doing is derived from the data (live vs the editions
# table), and a refresh is checked by a per-period before/after content hash in
# the same transaction.

def status(cur) -> dict:
    """What needs action between the live table and its editions (read-only).

    editions_core.status with SPEC (the row count expected of a period is
    _status_expected_rows), returned under the key names S1b has always used
    (s1_editions.format_status, cmd_sync_new and the verify gates read them):
    the core's 'drift' is 'drift_periods' and 'forked' is 'chain_errors'."""
    st = core.status(cur, SPEC)
    return {"new_periods": st["new_periods"], "drift_periods": st["drift"],
            "pending_refresh": st["pending_refresh"],
            "chain_errors": st["forked"], "bad_counts": st["bad_counts"],
            "periods": st["periods"], "ok": st["ok"]}


def _edition1_categories(cur, period) -> set:
    cur.execute(f"""SELECT DISTINCT category_code FROM public.{TABLE}
                    WHERE period = %s AND edition = 1""", (period,))
    return {r[0] for r in cur.fetchall()}


def _neighbour_with_editions(cur, period) -> "str | None":
    """The nearest earlier period that has editions (else the nearest later)."""
    cur.execute(f"SELECT MAX(period) FROM public.{TABLE} WHERE period < %s",
                (period,))
    prev = cur.fetchone()[0]
    if prev is not None:
        return prev
    cur.execute(f"SELECT MIN(period) FROM public.{TABLE} WHERE period > %s",
                (period,))
    return cur.fetchone()[0]


def _record_as_loaded(cur, period, expected, accept_categories=False) -> int:
    """Edition 1 'as loaded' for a live period that has no editions (same
    convention as backfill: canonical-text sha256 of the rows, source_file =
    source_edition, published_date = load date). Halts unless the live rows are
    `expected` authorities x their categories, share one loaded_at date,
    source_edition and source_url, and (unless accept_categories) have the
    category set of the nearest period that has editions. Returns the edition
    number."""
    cols = ", ".join(DATA_COLS)
    cur.execute(f"""SELECT {cols}, (loaded_at AT TIME ZONE 'UTC')::date
                    FROM public.{LIVE} WHERE period = %s""", (period,))
    fetched = cur.fetchall()
    rows = [dict(zip(DATA_COLS, r[:-1])) for r in fetched]
    auths = {r["lad24cd"] for r in rows}
    cats = {r["category_code"] for r in rows}
    if expected is not None and (len(auths) != expected
                                 or len(rows) != expected * len(cats)):
        halt(f"{period}: {len(auths)} authorities and {len(rows)} live rows "
             f"over {len(cats)} categories, expected {expected} authorities "
             f"and {expected * len(cats)} rows")
    nb = _neighbour_with_editions(cur, period)
    if nb is not None and not accept_categories:
        ref = _edition1_categories(cur, nb)
        if cats != ref:
            halt(f"{period}: category set differs from {nb} edition 1 (only "
                 f"here {sorted(cats - ref)[:4]}, only in {nb} "
                 f"{sorted(ref - cats)[:4]}); if the publisher changed the "
                 f"layout, re-run with --accept-categories {period}")
    loaded = {r[-1] for r in fetched}
    files = {r["source_edition"] for r in rows}
    urls = {r["source_url"] for r in rows}
    if len(loaded) != 1 or len(files) != 1 or len(urls) != 1:
        halt(f"{period}: live rows differ in loaded_at/source_edition/"
             f"source_url ({len(loaded)}/{len(files)}/{len(urls)})")
    return _insert(cur, rows, period, release_label=LIVE_LABEL,
                   published_date=loaded.pop(), source_file=files.pop(),
                   source_sha256=live_rows_sha256(rows), supersedes=None)


def sync_new(cur, expected_authorities_n=None, accept_categories=()) -> list:
    """Give every live period that has no editions its edition 1, 'as loaded'.
    Never touches a period that already has editions, so it is idempotent.
    Halts on a period whose authority or row count is not the one derived from
    the periods that have editions, or whose category set differs from the
    nearest such period's unless named in accept_categories. Returns the
    periods that were given an edition. If no live period has editions yet
    there is nothing to derive the count from, so expected_authorities_n (CLI
    --expected-authorities N) must be given; once a count can be derived N is
    accepted only if it equals it.

    S1b's own rather than editions_core.sync_new: the core has no category-set
    check, no single-source_url check, and hashes the rows without the period
    (rows_sha256), while S1b's 'as loaded' editions carry live_rows_sha256.
    The chain map and the insert are the core's (latest_map, _insert)."""
    tips, new, errors = latest_map(cur)
    if errors:
        halt("sync-new: invalid edition chain " + "; ".join(
            f"{p}: {m}" for p, m in errors.items()))
    known = {p: n for p, n in authority_counts(cur).items() if p not in new}
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
    stray = sorted(set(accept_categories) - set(new))
    if stray:
        halt(f"--accept-categories {stray}: not periods awaiting sync-new")
    done = []
    for p in sorted(new):
        _record_as_loaded(cur, p, expected, p in accept_categories)
        done.append(p)
    return done


def _plan(cur, accept_drift=()) -> tuple:
    """({period: {edition, kind, rows, one_sided}} for every period whose live
    rows differ from the latest edition, unaccepted drift periods). kind is
    'pending' (live equals an earlier edition) or 'drift' (it equals none).
    editions_core._plan with SPEC: same comparison (key + REFRESH_COLS), same
    halts and messages; one_sided is the core's addition."""
    return core._plan(cur, SPEC, accept_drift)


def _unrepairable(plan) -> list:
    """Planned periods that differ from their edition but have no row to
    update (rows missing from, or extra in, the live table): an UPDATE cannot
    repair them. S1b's own rule, kept for the refresh-latest preview so its
    output is unchanged; the core's refresh_latest also refuses a period with
    a key in only one side, which S1b's after-checks used to catch after the
    UPDATE (and roll back) instead."""
    return sorted(p for p, v in plan.items() if not v["rows"])


def refresh_counts(cur, accept_drift=()) -> dict:
    """{period: rows refresh_latest would write} (read-only)."""
    return core.refresh_counts(cur, SPEC, accept_drift)


def refresh_latest(cur, accept_drift=(), _after_update_hook=None) -> dict:
    """Copy the latest edition into the live table for every period whose live
    rows differ from it (NULL-safe); return a result dict.

    editions_core.refresh_latest with SPEC: updates ONLY value, value_flag,
    category_label, source_url, source_edition and edition_variant, on the
    rows of those periods that differ; every other column, including
    loaded_at, and every other period is untouched; the per-period content
    hash (excluding loaded_at) is taken before and after, a period not being
    refreshed must be identical and a refreshed one may differ only in the
    refresh columns. A period whose live rows equal no stored edition is
    drift: it halts unless named in accept_drift.

    Kept from S1b on top of the core: each refreshed period must then equal
    its latest edition on every column bar loaded_at (check_latest_equals_live,
    not only the refresh columns). The whole runs in a savepoint of S1b's own,
    so any failure, the core's or this one, rolls back to it and halts.
    _after_update_hook(cur) is a test seam that runs after the UPDATE and
    before the after-checks (the core passes the plan too; it is dropped).

    Result: {'updated': {period: rows}, 'rows': n, 'drift_accepted': [...]}.
    """
    hook = None
    if _after_update_hook is not None:
        def hook(c, _plan_unused):
            _after_update_hook(c)
    with core._own_savepoint(cur, "s1b_refresh_latest"):
        result = core.refresh_latest(cur, SPEC, tuple(accept_drift),
                                     _after_update_hook=hook)
        if result["updated"]:
            bad = [f"live!=latest: {x}" for x in
                   check_latest_equals_live(cur, sorted(result["updated"]))]
            if bad:
                halt("refresh-latest failed its before/after checks, rolled "
                     "back: " + "; ".join(bad[:6]))
    return result


def cmd_status(_args):
    from _db import get_readonly_conn
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            st = status(cur)
    finally:
        conn.close()
    print(format_status(st, "S1b support needs"))
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
            done = sync_new(cur, args.expected_authorities,
                            tuple(args.accept_categories or ()))
            for p in done:
                cur.execute(f"SELECT COUNT(*) FROM public.{TABLE} "
                            "WHERE period = %s AND edition = 1", (p,))
                print(f"  {p}: edition 1 recorded, {cur.fetchone()[0]} rows")
            st = status(cur)
            if st["new_periods"] or st["chain_errors"]:
                halt(f"after sync-new: {format_status(st, 'S1b')}")
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
                  f"{sorted(res['updated'])}; before/after guard passed")
            if res["drift_accepted"]:
                print(f"drift overwritten by request: {res['drift_accepted']}")
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
    bf = sub.add_parser("backfill", help="record edition 1 of stored quarters "
                        "and the 2025Q2 original and revised editions")
    mode = bf.add_mutually_exclusive_group()
    mode.add_argument("--commit", action="store_true",
                      help="insert the editions (append-only, irreversible)")
    mode.add_argument("--simulate", action="store_true",
                      help="run the full --commit path and roll back")
    bf.set_defaults(func=cmd_backfill)
    ld = sub.add_parser("load", help="extract a registry file and diff it "
                        "(dry-run); insert only with --commit")
    ld.add_argument("--period", required=True)
    ld.add_argument("--manifest-label", help="select the period's manifest "
                    "entry by release_label; only 'registry' loads")
    ld.add_argument("--manifest-entry", type=int,
                    help="alternative: 0-BASED index into the manifest")
    ld.add_argument("--supersedes", type=int, help="must equal the current "
                    "chain tip of the period (default: the tip)")
    ld.add_argument("--published-date", help="YYYY-MM-DD; default the "
                    "manifest last_modified date")
    lm = ld.add_mutually_exclusive_group()
    lm.add_argument("--commit", action="store_true",
                    help="insert the edition (append-only, irreversible)")
    lm.add_argument("--simulate", action="store_true",
                    help="run the full --commit path and roll back")
    ld.set_defaults(func=cmd_load)
    da = sub.add_parser("dryrun-all", help="markdown diff of all seven "
                        "registry files; writes no table")
    da.add_argument("--out", required=True)
    da.set_defaults(func=cmd_dryrun_all)
    rl = sub.add_parser("refresh-latest", help="copy each period's latest "
                        "edition into the live table (dry-run by default)")
    rm = rl.add_mutually_exclusive_group()
    rm.add_argument("--commit", action="store_true",
                    help="refresh the live table (before/after guard in the "
                    "same transaction)")
    rm.add_argument("--simulate", action="store_true",
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
    sn.add_argument("--accept-categories", action="append", metavar="PERIOD",
                    help="the category set of this new quarter really differs "
                    "from the previous quarter's (a layout change); repeatable")
    smode = sn.add_mutually_exclusive_group()
    smode.add_argument("--commit", action="store_true")
    smode.add_argument("--simulate", action="store_true",
                       help="do everything, then roll back")
    sn.set_defaults(func=cmd_sync_new)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
