"""Resolve stored lad24cd values to the pipeline's canonical boundary set.

The defect
----------
Barnsley and Sheffield were recoded by ONS: E08000016 -> E08000038 and
E08000019 -> E08000039. `la_boundaries` retains the old codes, and
`la_code_lookup` records the mapping in that direction - old_code E08000038,
new_code E08000016 - so the canonical code in this pipeline is the old one.

Sources that stored the publisher's code without resolving it produced rows
that Workflow 1 cannot join to. Not a halved figure: no matching row at all,
and therefore a NULL that W1's trend CASE then published as
'falling_strongly'. Two authorities of 296, in every run from 4 to 12.

Found by scanning all 40 tables carrying lad24cd:

    la_statutory_homelessness   split      TA NULL for both from 2025Q2
    la_rough_sleeping           new only   rough sleeping NULL for both, always
    nhs_mh_crfd                 split      masked by vw_mh_crfd_lad
    nhs_mh_crfd_repro           split      reproduction mirror of the above

Why nhs_mh_crfd is recoded too
------------------------------
It was not producing a wrong answer, because vw_mh_crfd_lad happens to
COALESCE through la_code_lookup on the way out. That is luck rather than
design - the same defect one view away from being live - and the new gate in
verify_lad24cd_canonical.py would fail on it regardless. Recoding the base
table does not break the view: after the recode, E08000016 no longer matches a
'recode' row, so the COALESCE falls back to the stored code, which is already
canonical.

nhs_mh_crfd_repro is the reproduction table that proved S9b rebuilds exactly.
It is recoded in the same transaction so the diff still holds; recoding one
and not the other would break the only evidence that S9b is reproducible.

Safety
------
Every recode is checked for primary-key collisions before anything is written,
and the script aborts if any exist. There are none today, because the old and
new codes occupy disjoint periods in all four tables - MHSDS switched in June
2025, homelessness in 2025Q2 - but that is verified rather than assumed, since
a silent collision would drop rows on a unique index.

Idempotent: a second run finds nothing to do, because a recoded row's code no
longer differs from its canonical resolution.

Usage:
    python scripts/fix_lad24cd_resolve_to_canonical.py --check
    python scripts/fix_lad24cd_resolve_to_canonical.py --apply
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _db import get_conn  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# table -> the rest of its primary key, used for the collision test
TABLES = {
    "la_statutory_homelessness": ["period"],
    "la_rough_sleeping": ["snapshot_year"],
    "nhs_mh_crfd": ["reporting_period", "measure_id"],
    "nhs_mh_crfd_repro": ["reporting_period", "measure_id"],
}


def pending(cur, table):
    cur.execute(f"""
        SELECT t.lad24cd, l.new_code, COUNT(*)
        FROM {table} t
        JOIN la_code_lookup l ON l.old_code = t.lad24cd
        WHERE l.new_code IS DISTINCT FROM t.lad24cd
        GROUP BY 1, 2 ORDER BY 1
    """)
    return cur.fetchall()


def collisions(cur, table, keycols):
    on = " AND ".join(f"b.{k} = a.{k}" for k in keycols)
    cur.execute(f"""
        SELECT a.lad24cd, l.new_code, COUNT(*)
        FROM {table} a
        JOIN la_code_lookup l ON l.old_code = a.lad24cd
                             AND l.new_code IS DISTINCT FROM a.lad24cd
        JOIN {table} b ON b.lad24cd = l.new_code AND {on}
        GROUP BY 1, 2
    """)
    return cur.fetchall()


def orphans(cur, table):
    """Codes that resolve to nothing in la_boundaries after the recode."""
    cur.execute(f"""
        SELECT DISTINCT t.lad24cd FROM {table} t
        WHERE NOT EXISTS (SELECT 1 FROM la_boundaries b
                          WHERE b.lad24cd = t.lad24cd)
        ORDER BY 1
    """)
    return [r[0] for r in cur.fetchall()]


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
        print("Pending recodes and collision test:")
        blocked, work = [], {}
        for table, keycols in TABLES.items():
            todo = pending(cur, table)
            coll = collisions(cur, table, keycols)
            work[table] = todo
            n = sum(r[2] for r in todo)
            print(f"  {table:<28} {n:>4} row(s) to recode  "
                  f"{[f'{a}->{b} ({c})' for a, b, c in todo] or 'none'}")
            if coll:
                blocked.append(f"{table}: {coll}")
        if blocked:
            conn.rollback()
            sys.exit("HALT: primary-key collisions would drop rows:\n  " +
                     "\n  ".join(blocked))
        print("  collisions: none — old and new codes occupy disjoint periods")
        print()

        if not args.apply:
            conn.rollback()
            print("--check only, nothing written.")
            return 0

        for table in TABLES:
            cur.execute(f"""
                UPDATE {table} t
                SET lad24cd = l.new_code
                FROM la_code_lookup l
                WHERE l.old_code = t.lad24cd
                  AND l.new_code IS DISTINCT FROM t.lad24cd
            """)
            print(f"  {table:<28} {cur.rowcount:>4} row(s) recoded")

        problems = []
        for table in TABLES:
            left = pending(cur, table)
            if left:
                problems.append(f"{table} still has unresolved codes: {left}")
            orph = orphans(cur, table)
            if orph:
                problems.append(f"{table} holds code(s) absent from "
                                f"la_boundaries: {orph}")
        if problems:
            conn.rollback()
            sys.exit("HALT: post-recode assertions failed:\n  " +
                     "\n  ".join(problems))

        conn.commit()
        print("\nAll four tables resolve to la_boundaries. COMMITTED")
        return 0
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
