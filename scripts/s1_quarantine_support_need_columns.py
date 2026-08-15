"""Quarantine the five mis-mapped support-need columns on la_statutory_homelessness.

Why this is a data change and not a documentation change
--------------------------------------------------------
Discovery for S1b established that five columns on la_statutory_homelessness
do not contain what their names say. The offset differs by quarter, so this is
not one bug with one shape:

    2025Q2   mental_health          holds Care leaver aged 21-24
             learning_disability    holds Care leaver aged 25+
             drug_dependency        holds Learning disability
             alcohol_dependency     holds Sexual abuse / exploitation
             rough_sleeping_history holds Drug dependency

    2025Q3   drug_dependency        holds Alcohol dependency
             alcohol_dependency     holds Offending history
             rough_sleeping_history holds History of repeat homelessness

The 2025Q2 case is provable rather than inferred: homelessness_quarter_urls
names the exact asset that quarter was loaded from, and it is byte-identical
to the file the S1b build reads, so no revision can account for the
difference. Middlesbrough's true mental health figure is 297; the column
called mental_health holds 6.

A decision record does not protect anyone from a column called mental_health
that contains care-leaver counts. The next query written against this table
will read it at face value, and 6 against a true 297 is exactly the kind of
figure that looks plausible enough to survive review.

What this does
--------------
Renames each column to <name>_suspect and sets every value to NULL.

Both halves matter and they do different jobs. The rename makes any existing
query fail loudly with "column does not exist" rather than silently returning
wrong numbers - loud failure is the whole point. Nulling means that even a
query updated to the new name gets nothing rather than a plausible-looking
wrong figure.

Nothing is lost. la_homelessness_support_needs (S1b) holds all 24 published
A3 support-need categories, correctly labelled, for every quarter S1 covers
and two more besides, with per-row provenance. The publisher files are
re-fetchable and the build script is committed.

support_needs_total is deliberately left alone. It is the one column that is
correctly mapped - it holds the publisher's "households with one or more
support needs", verified against the same file at 291/296 and 294/296 for the
two quarters where the loaded file and the published file are the same asset.

DDL policy
----------
The repository's rule is additive DDL. A rename is neither additive nor
reversible-by-default, so it is taken here as a deliberate corrective
migration under the same bounded-exception test the source registry applies to
widening a CHECK constraint:

  1. It is guarded and idempotent - the DO block checks for both the old and
     the new name, so a re-run is a no-op.
  2. It is reversible - the inverse rename is one statement, and the values it
     would restore are reproducible from the publisher files via S1b.
  3. It is asserted afterwards - the script re-reads the catalogue and fails
     if the columns are not in the expected state.

Usage:
    python scripts/s1_quarantine_support_need_columns.py --check
    python scripts/s1_quarantine_support_need_columns.py --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TABLE = "la_statutory_homelessness"
COLUMNS = ["mental_health", "learning_disability", "drug_dependency",
           "alcohol_dependency", "rough_sleeping_history"]

MIGRATE = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = %(table)s AND column_name = %(old)s)
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = %(table)s AND column_name = %(new)s)
    THEN
        EXECUTE format('ALTER TABLE %%I RENAME COLUMN %%I TO %%I',
                       %(table)s, %(old)s, %(new)s);
    END IF;
END $$;
"""


def state(cur):
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = %s ORDER BY ordinal_position
    """, (TABLE,))
    return [r[0] for r in cur.fetchall()]


def report(cur):
    cols = state(cur)
    print(f"{TABLE} columns:")
    for c in cols:
        mark = ""
        if c in COLUMNS:
            mark = "   <-- mis-mapped, not yet quarantined"
        elif c.endswith("_suspect"):
            cur.execute(f"SELECT COUNT({c}) FROM {TABLE}")
            mark = f"   <-- quarantined, {cur.fetchone()[0]} non-null value(s) remaining"
        print(f"  {c}{mark}")
    return cols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if not (args.check or args.apply):
        ap.error("choose --check or --apply")

    conn = get_conn()
    cur = conn.cursor()
    try:
        print("Before:")
        report(cur)
        print()

        if not args.apply:
            conn.rollback()
            print("--check only, nothing written.")
            return 0

        for old in COLUMNS:
            new = f"{old}_suspect"
            cur.execute(MIGRATE, {"table": TABLE, "old": old, "new": new})
            cur.execute(f"UPDATE {TABLE} SET {new} = NULL WHERE {new} IS NOT NULL")
            print(f"  {old} -> {new}, {cur.rowcount} value(s) nulled")

        # Assert the end state rather than trusting the statements ran.
        cols = state(cur)
        problems = []
        for old in COLUMNS:
            new = f"{old}_suspect"
            if old in cols:
                problems.append(f"{old} still present under its original name")
            if new not in cols:
                problems.append(f"{new} was not created")
            else:
                cur.execute(f"SELECT COUNT({new}) FROM {TABLE}")
                remaining = cur.fetchone()[0]
                if remaining:
                    problems.append(f"{new} still holds {remaining} value(s)")
        if "support_needs_total" not in cols:
            problems.append("support_needs_total was touched and should not have been")
        if problems:
            conn.rollback()
            sys.exit("HALT: post-migration assertions failed:\n  " +
                     "\n  ".join(problems))

        conn.commit()
        print("\nAfter:")
        report(cur)
        print("\nCOMMITTED")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
