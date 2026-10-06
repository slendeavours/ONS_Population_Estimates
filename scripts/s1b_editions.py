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
    python scripts/s1b_editions.py load ...            # Task 3 (not yet built)
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

from s1_editions import Q2_FILES, halt, sha256_file  # noqa: E402

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
    """Tip of the period's supersedes chain (the edition no other edition of
    the period supersedes). Raises LookupError if the period has no editions
    and ValueError if the chain is not linear: several roots, a fork (several
    tips), inconsistent supersedes within an edition, or a cycle."""
    cur.execute(f"SELECT DISTINCT edition, supersedes FROM public.{TABLE} "
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
    if len(sup) == 1:
        return next(iter(sup))
    roots = sorted(e for e, s in sup.items() if s is None)
    if len(roots) != 1:
        raise ValueError(f"{period}: {len(roots)} editions {roots} have no "
                         "supersedes while several editions exist; the "
                         "chain has no single root")
    superseded = {s for s in sup.values() if s is not None}
    tips = sorted(e for e in sup if e not in superseded)
    if len(tips) != 1:
        raise ValueError(f"{period}: supersedes chain has {len(tips)} tips "
                         f"{tips}; order is ambiguous (fork or cycle)")
    return tips[0]


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
    if len(csv_rows) != 9176:
        bad.append(f"CSV holds {len(csv_rows)} distinct rows, expected 9176")
    if len(ed1) != 9176:
        bad.append(f"2025Q2 edition 1 holds {len(ed1)} rows, expected 9176")
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
    filename = path.name.split("_", 1)[1]
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
            out.append(("2025Q2", None, 9176, "would insert"))
            continue
        edition = _q2_edition(cur, path, urls[fname])
        df = s1b.read_a3(path)
        _, _, tuples = s1b.build_rows(cur, edition, df)
        rows = [dict(zip(DATA_COLS, t)) for t in tuples]
        if len(rows) != 9176:
            halt(f"{fname}: {len(rows)} rows extracted, expected 9176")
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


def cmd_pending(args):
    halt(f"'{args.cmd}' is not built yet (Task "
         f"{'3' if args.cmd == 'load' else '4'} of the S1b edition plan)")


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
    for name in ("load", "refresh-latest"):
        p = sub.add_parser(name, help="not built yet")
        m = p.add_mutually_exclusive_group()
        m.add_argument("--commit", action="store_true")
        m.add_argument("--simulate", action="store_true")
        p.set_defaults(func=cmd_pending)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
