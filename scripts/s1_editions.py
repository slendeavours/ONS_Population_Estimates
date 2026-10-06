"""S1 edition history: append-only table of every published edition of each quarter.

la_statutory_homelessness holds one row per LA per quarter (the latest layer).
This module owns la_statutory_homelessness_editions, which keeps every
published edition of a quarter so a revision never overwrites what was sent.
Rows are immutable: a trigger raises on UPDATE, DELETE and TRUNCATE.

Subcommands (backfill, load, refresh-latest and diff are added in later steps):
    python scripts/s1_editions.py ddl     # idempotent; safe to run repeatedly

Helpers imported by later steps: sha256_file, create_schema, insert_edition,
latest_edition.
"""
import argparse
import hashlib
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
    cur.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON public.{TABLE}")
    cur.execute(f"""CREATE TRIGGER {TRIGGER}
        BEFORE UPDATE OR DELETE ON public.{TABLE}
        FOR EACH ROW EXECUTE FUNCTION public.{TRIGGER}()""")
    cur.execute(f"DROP TRIGGER IF EXISTS {TRUNCATE_TRIGGER} ON public.{TABLE}")
    cur.execute(f"""CREATE TRIGGER {TRUNCATE_TRIGGER}
        BEFORE TRUNCATE ON public.{TABLE}
        FOR EACH STATEMENT EXECUTE FUNCTION public.{TRIGGER}()""")


def insert_edition(cur, recs: list, period: str, *, release_label: str,
                   published_date: "date | None", source_url: "str | None",
                   source_file: str, source_sha256: str,
                   supersedes: "int | None") -> int:
    """Insert one edition of a period; return its edition number.

    If source_sha256 is already recorded for the period nothing is inserted and
    the existing edition number is returned. Missing measures are stored NULL.
    """
    cur.execute(f"SELECT DISTINCT edition FROM public.{TABLE} "
                "WHERE period = %s AND source_sha256 = %s",
                (period, source_sha256))
    existing = [r[0] for r in cur.fetchall()]
    if existing:
        return existing[0]
    cur.execute(f"SELECT COALESCE(MAX(edition), 0) FROM public.{TABLE} "
                "WHERE period = %s", (period,))
    edition = cur.fetchone()[0] + 1
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
    top = max((d for _, d in eds if d is not None), default=None)
    if top is None:
        if len(eds) > 1:
            raise ValueError(f"{period}: several editions, none dated")
        return eds[0][0]
    tied = sorted(e for e, d in eds if d == top)
    if len(tied) > 1 and _values_differ(cur, period, tied[0], tied[-1]):
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ddl", help="create the table and triggers if absent"
                   ).set_defaults(func=cmd_ddl)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
