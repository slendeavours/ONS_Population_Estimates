"""S1b edition history: append-only table of every published edition of each
quarter of the Table A3 support-needs data.

la_homelessness_support_needs holds one row per authority per category per
quarter (the latest layer). This module owns
la_homelessness_support_needs_editions, which keeps every published edition of
a quarter so a revision never overwrites what was sent. Rows are immutable:
triggers raise on UPDATE, DELETE and TRUNCATE. It mirrors s1_editions.py.

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
    python scripts/s1b_editions.py refresh-latest ...  # Task 4 (not yet built)

Helpers imported by later steps: create_schema, insert_edition, latest_edition,
check_coverage, check_latest_equals_live, check_q2_pair.
"""
import argparse
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from s1_editions import (Q2_FILES, STALE_PERIODS, halt,  # noqa: E402
                         load_manifest, select_entry, sha256_file)
from s1_editions import latest_edition as _s1_latest_edition  # noqa: E402

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
EXTRA_COLS = ("release_label", "published_date", "source_file",
              "source_sha256", "supersedes")

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

EDITION_ROWS_TOTAL = 101232   # edition 1 of the eleven quarters
BACKFILL_ROWS_TOTAL = 110408  # plus 2025Q2 edition 2


def expected_rows(period) -> int:
    """296 authorities x 31 categories (32 in the 2025Q4 layout)."""
    return 296 * (32 if period == "2025Q4" else 31)


def create_schema(cur) -> None:
    """Create the table and immutability triggers if absent. Idempotent."""
    t = TABLE
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS public.{t} (
        lad24cd           varchar(9)  NOT NULL
            REFERENCES public.la_boundaries (lad24cd),
        period            varchar(6)  NOT NULL
            CONSTRAINT {t}_period_chk CHECK (period ~ '^\\d{{4}}Q[1-4]$'),
        category_code     text        NOT NULL,
        edition           integer     NOT NULL,
        value             integer,
        value_flag        text
            CONSTRAINT {t}_value_flag_chk CHECK (value_flag IS NULL
                OR value_flag IN ('missing','suppressed','not_applicable')),
        category_group    text        NOT NULL
            CONSTRAINT {t}_category_group_chk CHECK (category_group IN
                ('support_need','needs_breakdown','needs_total','duty_total')),
        category_label    text        NOT NULL,
        reference_quarter varchar(7)  NOT NULL,
        source_url        text        NOT NULL,
        source_edition    text        NOT NULL,
        edition_variant   text        NOT NULL
            CONSTRAINT {t}_edition_variant_chk CHECK (edition_variant IN
                ('original','revised','corrected','fixed')),
        release_page_url  text        NOT NULL,
        layout_version    text        NOT NULL,
        publisher_la_code varchar(9)  NOT NULL,
        loaded_at         timestamptz NOT NULL DEFAULT now(),
        release_label     text,
        published_date    date,
        source_file       text,
        source_sha256     text,
        supersedes        integer,
        CONSTRAINT {t}_value_xor_flag_chk
            CHECK (num_nonnulls(value, value_flag) = 1),
        PRIMARY KEY (lad24cd, period, category_code, edition)
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


def insert_edition(cur, rows: list, period: str, *, release_label: str,
                   published_date, source_url, source_file: str,
                   source_sha256: str, supersedes) -> int:
    """Insert one edition of a period; return its edition number.

    rows are dicts keyed by DATA_COLS. If source_sha256 is already recorded for
    the period nothing is inserted and the existing edition number is returned.
    Empty rows is a hard stop, and so is a `supersedes` that is not an existing
    edition of the period or a row whose period differs. source_url overrides
    the rows' own source_url only when given (None keeps the row value).
    """
    from psycopg2.extras import execute_values
    if not rows:
        halt(f"insert_edition: no rows supplied for {period}")
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
    if any(r["period"] != period for r in rows):
        halt(f"insert_edition: rows contain a period other than {period}")
    cols = list(DATA_COLS) + ["edition"] + list(EXTRA_COLS)
    data = []
    for r in rows:
        vals = [r[c] for c in DATA_COLS]
        if source_url is not None:
            vals[DATA_COLS.index("source_url")] = source_url
        data.append(vals + [edition, release_label, published_date,
                            source_file, source_sha256, supersedes])
    execute_values(cur, f"INSERT INTO public.{TABLE} ({', '.join(cols)}) "
                   "VALUES %s", data, page_size=2000)
    return edition


def latest_edition(cur, period: str) -> int:
    """Chain tip of the period in the S1b editions table; the validation
    (single root, no fork, no self/dangling supersedes, whole chain reachable)
    lives in s1_editions.latest_edition."""
    return _s1_latest_edition(cur, period, table=TABLE)


# ---------------------------------------------------------------- checks

def check_coverage(cur) -> tuple:
    """Gate 4: every live period has edition 1; every edition has 296
    authorities and the period's expected row count. -> (problems, rows in
    edition 1, rows in table)."""
    cur.execute(f"SELECT DISTINCT period FROM public.{LIVE}")
    periods = sorted(r[0] for r in cur.fetchall())
    cur.execute(f"""SELECT period, edition, COUNT(DISTINCT lad24cd), COUNT(*)
                    FROM public.{TABLE} GROUP BY 1, 2""")
    eds = {}
    for p, e, n, rows in cur.fetchall():
        eds.setdefault(p, {})[e] = (n, rows)
    bad, n1 = [], 0
    for p in periods:
        e = eds.get(p, {})
        if 1 not in e:
            bad.append(f"{p}: no edition 1")
        n1 += e.get(1, (0, 0))[1]
    for p, e in sorted(eds.items()):
        want = (296, expected_rows(p))
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
    cols = ", ".join(DATA_COLS)
    cur.execute(f"SELECT DISTINCT period FROM public.{LIVE}")
    live_periods = sorted(r[0] for r in cur.fetchall())
    bad = []
    for p in (periods or live_periods):
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
    want = expected_rows("2025Q2")
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
        ed = insert_edition(cur, rows, period, release_label=LIVE_LABEL,
                            published_date=loaded.pop(), source_url=None,
                            source_file=files.pop(),
                            source_sha256=live_rows_sha256(rows),
                            supersedes=None)
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
            out.append(("2025Q2", None, expected_rows("2025Q2"), "would insert"))
            continue
        edition = _q2_edition(cur, path, urls[fname])
        df = s1b.read_a3(path)
        _, _, tuples = s1b.build_rows(cur, edition, df)
        rows = [dict(zip(DATA_COLS, t)) for t in tuples]
        if len(rows) != expected_rows("2025Q2"):
            halt(f"{fname}: {len(rows)} rows extracted, expected "
                 f"{expected_rows('2025Q2')}")
        ed = insert_edition(cur, rows, "2025Q2", release_label=label,
                            published_date=published, source_url=None,
                            source_file=edition["filename"], source_sha256=sha,
                            supersedes=prev if sup is not None else None)
        prev = ed
        out.append(("2025Q2", ed, _count(cur, "2025Q2", ed), "inserted"))
    return out


def run_backfill_gates(cur) -> None:
    """Gates 4-5 plus the 2025Q2 pair and the row total; halt on any problem."""
    bad, n1, _ = check_coverage(cur)
    if n1 != EDITION_ROWS_TOTAL:
        bad.append(f"edition 1 holds {n1} rows, expected {EDITION_ROWS_TOTAL}")
    cur.execute(f"""SELECT COUNT(*) FROM public.{TABLE}
                    WHERE edition = 1 OR (period = '2025Q2' AND edition = 2)""")
    n = cur.fetchone()[0]
    if n != BACKFILL_ROWS_TOTAL:
        bad.append(f"backfill rows {n}, expected {BACKFILL_ROWS_TOTAL}")
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
    """Gate 6 for one edition: 296 authorities and the period's category rows,
    every lad24cd inside la_code_lookup, a source_url, the same category set as
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
    if (n, len(rows)) != (296, expected_rows(period)):
        bad.append(f"{tag}: {n} authorities/{len(rows)} rows, expected "
                   f"296/{expected_rows(period)}")
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
    None/blank -> missing; a documented marker -> its flag; else the number."""
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
    if len(stored) != expected_rows(period):
        bad.append(f"{tag}: {len(stored)} stored cells, expected "
                   f"{expected_rows(period)}")
    return bad, len(stored), zeros


def check_s1_cross(cur, period, edition, s1=None) -> list:
    """Gate 8 for one edition: the 'one or more support needs' value of each
    authority equals la_statutory_homelessness.support_needs_total for the same
    lad24cd and period, NULL-safe, 296/296; a flagged S1b cell against a
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
    if n != 296 or len(got) != 296:
        return [f"{tag}: {n}/296 equal ({len(got)} stored rows)"
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
            ed = insert_edition(
                cur, rows, args.period, release_label="registry",
                published_date=published, source_url=None,
                source_file=edition["filename"], source_sha256=sha,
                supersedes=prev)
            if latest_edition(cur, args.period) != ed:
                halt(f"{args.period}: new edition {ed} is not the chain tip")
            run_load_gates(cur, args.period, ed)
        if args.commit:
            conn.commit()
            print(f"loaded {args.period} edition {ed} "
                  f"({expected_rows(args.period)} rows); gates 6-8 passed in "
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
                        f"{expected_rows(p) // 296}, {expected_rows(p)}); "
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


def cmd_pending(args):
    halt(f"'{args.cmd}' is not built yet (Task 4 of the S1b edition plan)")


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
    rl = sub.add_parser("refresh-latest", help="not built yet (Task 4)")
    rm = rl.add_mutually_exclusive_group()
    rm.add_argument("--commit", action="store_true")
    rm.add_argument("--simulate", action="store_true")
    rl.set_defaults(func=cmd_pending)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
