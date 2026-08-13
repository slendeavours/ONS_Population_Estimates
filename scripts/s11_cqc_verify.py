"""
s11_cqc_verify.py — S11 Phase 4: Post-load verification checks.

Purpose : Runs the gate checks against cqc_locations after a load:
          1. table row count matches the processed/mapped CSV row count
          2. zero null lad24cd; every lad24cd exists in la_boundaries;
             fallback-mapped rows counted and listed
          3. all 296 LAs accounted for - locations present or zero-count listed
          4. target-market sanity: supported-living counts for Birmingham,
             Liverpool, Nottingham, Manchester, Blackpool
Inputs  : data/processed/cqc_locations_mapped.csv, exempt_pipeline tables
Outputs : PASS/FAIL per check on stdout, non-zero exit on any FAIL.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "processed" / "cqc_locations_mapped.csv"


def pg_conn():
    """Connect using PG_* values from the repo .env (never hardcoded)."""
    env = {}
    # .env sits at the working-copy root: this repository root in the
    # outer checkout, one level up inside the published one. Same
    # two-location lookup as scripts/_db.py.
    env_file = next((p for p in (ROOT / ".env", ROOT.parent / ".env")
                     if p.exists()), ROOT / ".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    password = os.environ.get("PG_PASSWORD") or env.get("PG_PASSWORD")
    if not password:
        sys.exit("PG_PASSWORD not set (env var or .env) - refusing to guess")
    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "localhost"), port=5432,
        dbname=env.get("PG_DATABASE", "exempt_pipeline"),
        user=os.environ.get("PG_USER") or env.get("PG_USER"), password=password)


def main():
    failures = []
    csv_rows = len(pd.read_csv(SRC, usecols=["location_id"]))
    conn = pg_conn()
    cur = conn.cursor()

    # 1. row count
    cur.execute("SELECT COUNT(*) FROM cqc_locations WHERE is_active")
    active = cur.fetchone()[0]
    ok = active == csv_rows
    print(f"check1 row_count: table_active={active} csv={csv_rows} "
          f"{'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append("row_count")

    # 2. lad24cd integrity + fallback listing
    cur.execute("SELECT COUNT(*) FROM cqc_locations WHERE lad24cd IS NULL")
    nulls = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM cqc_locations c
                   LEFT JOIN la_boundaries b USING (lad24cd)
                   WHERE b.lad24cd IS NULL""")
    orphans = cur.fetchone()[0]
    ok = nulls == 0 and orphans == 0
    print(f"check2 lad24cd: nulls={nulls} not_in_la_boundaries={orphans} "
          f"{'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append("lad24cd")
    cur.execute("""SELECT mapping_method, COUNT(*) FROM cqc_locations
                   GROUP BY mapping_method ORDER BY 2 DESC""")
    for method, n in cur.fetchall():
        print(f"  mapping_method {method}: {n}")
    cur.execute("""SELECT location_id, location_name, lad24cd, mapping_method
                   FROM cqc_locations WHERE mapping_method <> 'point_in_polygon'
                   ORDER BY mapping_method""")
    for r in cur.fetchall():
        print(f"  fallback row: {r}")

    # 3. LA coverage
    cur.execute("""SELECT b.lad24cd, b.lad24nm FROM la_boundaries b
                   LEFT JOIN cqc_locations c
                     ON c.lad24cd = b.lad24cd AND c.is_active
                   WHERE c.location_id IS NULL""")
    zero = cur.fetchall()
    print(f"check3 coverage: {296 - len(zero)}/296 LAs have locations; "
          f"{len(zero)} with zero:")
    for code, name in zero:
        print(f"  zero-count LA: {code} {name}")

    # 4. target-market sanity (supported living, active, non-dormant)
    cur.execute("""SELECT b.lad24nm, b.lad24cd, COUNT(*)
                   FROM cqc_locations c JOIN la_boundaries b USING (lad24cd)
                   WHERE c.supported_living AND c.is_active AND NOT c.dormant
                     AND b.lad24nm IN ('Birmingham','Liverpool','Nottingham',
                                       'Manchester','Blackpool')
                   GROUP BY 1, 2 ORDER BY 3 DESC""")
    for name, code, n in cur.fetchall():
        print(f"check4 {name} ({code}): supported_living={n}")

    conn.close()
    if failures:
        sys.exit(f"FAILED checks: {failures}")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
