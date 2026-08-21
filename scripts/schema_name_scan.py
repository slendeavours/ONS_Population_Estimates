r"""Scan the database schema and the tracked tree for a name that must not appear.

Written after the S20 neutral-naming migration was found incomplete a day after
it was recorded closed. The migration's own check queried
information_schema.tables and information_schema.columns, declared the schema
clean, and missed three objects: two primary key names and a column default that
kept writing the name into every new row. A primary key name is printed by
\d, so the one command anyone runs to understand a table disclosed what the
rename existed to hide.

The lesson is not that those three object classes were forgotten. It is that a
scan reconstructed by hand covers whatever the author thought of that morning.
This covers every class that can carry a name, so the coverage is stated once
and is the same on every run.

No term is hardcoded. Embedding the counterparty's name in a tracked file is
the disclosure this guards against, so terms are passed as arguments or held in
an untracked file.

Usage:
    python scripts/schema_name_scan.py <term> [<term> ...]
    python scripts/schema_name_scan.py --terms-file .name_scan_terms

Exit 0 clean, 1 on any hit.
"""
import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_readonly_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent

# Every object class that can carry a name. Each entry returns one column
# describing the hit. Adding a class here is how coverage grows; nothing is
# left to the caller to remember.
QUERIES = {
    "table": """
        SELECT table_schema || '.' || table_name
        FROM information_schema.tables WHERE table_name ILIKE %(p)s""",
    "column": """
        SELECT table_name || '.' || column_name
        FROM information_schema.columns WHERE column_name ILIKE %(p)s""",
    "column default": """
        SELECT table_name || '.' || column_name || '  DEFAULT ' || column_default
        FROM information_schema.columns WHERE column_default ILIKE %(p)s""",
    "constraint": """
        SELECT conrelid::regclass || ' -> ' || conname
        FROM pg_constraint WHERE conname ILIKE %(p)s""",
    "index": """
        SELECT schemaname || '.' || indexname
        FROM pg_indexes WHERE indexname ILIKE %(p)s""",
    "view name": """
        SELECT schemaname || '.' || viewname
        FROM pg_views WHERE viewname ILIKE %(p)s""",
    "view definition": """
        SELECT schemaname || '.' || viewname
        FROM pg_views WHERE definition ILIKE %(p)s""",
    "materialized view": """
        SELECT schemaname || '.' || matviewname
        FROM pg_matviews WHERE matviewname ILIKE %(p)s OR definition ILIKE %(p)s""",
    "sequence": """
        SELECT schemaname || '.' || sequencename
        FROM pg_sequences WHERE sequencename ILIKE %(p)s""",
    "comment": """
        SELECT c.relname || '  -- ' || obj_description(c.oid)
        FROM pg_class c WHERE obj_description(c.oid) ILIKE %(p)s""",
    "function": """
        SELECT n.nspname || '.' || p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
          AND (p.proname ILIKE %(p)s OR p.prosrc ILIKE %(p)s)""",
    "trigger": """
        SELECT tgrelid::regclass || ' -> ' || tgname
        FROM pg_trigger WHERE NOT tgisinternal AND tgname ILIKE %(p)s""",
    "role": """
        SELECT rolname FROM pg_roles WHERE rolname ILIKE %(p)s""",
}


def scan_schema(terms):
    hits = []
    conn = get_readonly_conn()
    try:
        with conn.cursor() as cur:
            for term in terms:
                pattern = f"%{term}%"
                for label, sql in QUERIES.items():
                    cur.execute(sql, {"p": pattern})
                    for (found,) in cur.fetchall():
                        hits.append((term, label, found))
    finally:
        conn.rollback()
        conn.close()
    return hits


def scan_tracked_files(terms):
    """Tracked files only. Untracked working files are expected to carry it."""
    hits = []
    for term in terms:
        result = subprocess.run(
            ["git", "grep", "-i", "-n", "--", term],
            cwd=REPO, capture_output=True, text=True, errors="replace")
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                hits.append((term, "tracked file", line))
        elif result.returncode != 1:
            sys.exit(f"HARD STOP: git grep failed: {result.stderr.strip()}")
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("terms", nargs="*", help="names that must not appear")
    ap.add_argument("--terms-file", help="file of terms, one per line")
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--files-only", action="store_true")
    args = ap.parse_args()

    terms = list(args.terms)
    if args.terms_file:
        path = Path(args.terms_file)
        if not path.is_absolute():
            path = REPO / path
        if not path.exists():
            sys.exit(f"HARD STOP: terms file not found: {path}")
        terms += [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
    if not terms:
        sys.exit("HARD STOP: no terms given. Pass them as arguments or with "
                 "--terms-file. This script will not guess what to look for.")

    hits = []
    if not args.files_only:
        hits += scan_schema(terms)
    if not args.schema_only:
        hits += scan_tracked_files(terms)

    # Terms are echoed only as a count. Printing them would put the thing being
    # guarded into any log or CI output that captures this run.
    scope = ("schema" if args.files_only is False and args.schema_only else
             "tracked files" if args.files_only else "schema + tracked files")
    print(f"Scanned {scope} for {len(terms)} term(s), "
          f"{len(QUERIES)} object classes.")

    if not hits:
        print("CLEAN: no hits.")
        return 0

    print(f"\n{len(hits)} HIT(S):")
    for term, label, found in hits:
        print(f"  [{label}] {found}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
