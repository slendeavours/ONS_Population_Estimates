"""S1 edition history: append-only table of every published edition of each quarter.

la_statutory_homelessness holds one row per LA per quarter (the latest layer).
This module owns la_statutory_homelessness_editions, which keeps every
published edition of a quarter so a revision never overwrites what was sent.
Rows are immutable: a trigger raises on UPDATE, DELETE and TRUNCATE.

Subcommands (load, refresh-latest and diff are added in later steps):
    python scripts/s1_editions.py ddl       # idempotent; safe to run repeatedly
    python scripts/s1_editions.py backfill  # edition 1 of every stored quarter
                                            # plus the 2025Q2 revision; idempotent

Helpers imported by later steps: sha256_file, create_schema, insert_edition,
latest_edition.
"""
import argparse
import hashlib
import csv
import sys
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


def _values_differ(cur, period, ed_a, ed_b) -> bool:
    cols = ", ".join(("lad24cd",) + MEASURES)
    q = (f"SELECT COUNT(*) FROM (SELECT {cols} FROM public.{TABLE} "
         "WHERE period = %s AND edition = %s EXCEPT "
         f"SELECT {cols} FROM public.{TABLE} "
         "WHERE period = %s AND edition = %s) x")
    cur.execute(q, (period, ed_a, period, ed_b))
    a = cur.fetchone()[0]
    cur.execute(q, (period, ed_b, period, ed_a))
    return bool(a or cur.fetchone()[0])


def latest_edition(cur, period: str) -> int:
    """Edition with the highest published_date (ties: highest edition).

    Raises if two editions share the top published_date but differ in values,
    because the order is then unknowable and must not be guessed.
    """
    cur.execute(f"SELECT DISTINCT edition, published_date FROM public.{TABLE} "
                "WHERE period = %s", (period,))
    eds = cur.fetchall()
    if not eds:
        raise LookupError(f"no editions recorded for {period}")
    dated = [d for _, d in eds if d is not None]
    if dated and len(dated) != len(eds):
        undated = sorted(e for e, d in eds if d is None)
        raise ValueError(f"{period}: editions {undated} have no "
                         "published_date while others are dated; "
                         "order is ambiguous")
    if not dated:
        if len(eds) > 1:
            raise ValueError(f"{period}: several editions, none dated")
        return eds[0][0]
    top = max(dated)
    tied = sorted(e for e, d in eds if d == top)
    for other in tied[1:]:
        if _values_differ(cur, period, tied[0], other):
            raise ValueError(f"{period}: editions {tied} share published_date "
                             f"{top} and differ in values; order is ambiguous")
    return tied[-1]


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
        if n != 296:
            halt(f"{period} edition {ed}: {n} rows, expected 296")
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddl", help="create the table and triggers if absent"
                   ).set_defaults(func=cmd_ddl)
    sub.add_parser("backfill", help="record edition 1 of stored quarters and "
                   "the 2025Q2 revision").set_defaults(func=cmd_backfill)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
