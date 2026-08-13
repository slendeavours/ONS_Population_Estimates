"""
s18_pipr_transform.py — S18 Phase 3: Transform PIPR workbook to load-ready CSV.

Purpose : Extract England LA rows from Table 1, reshape wide -> long
          (one row per lad24cd/period/breakdown_type/category), resolve every
          area code through la_code_lookup, apply the CHD-verified 2025
          Barnsley/Sheffield recode mapping, flag provisional periods, and
          write the processed dataset.
Inputs  : data/raw/pipr_<edition>.xlsx (argv[1] edition slug, default 17june2026)
          la_code_lookup in Postgres exempt_pipeline (localhost:5432)
          MIN_PERIOD env var (default 2024-03-01)
Outputs : data/processed/la_private_rents_<edition>.csv
          stdout: reconciliation counts, unresolved-code report.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parent.parent
EDITION = sys.argv[1] if len(sys.argv) > 1 else "17june2026"
MIN_PERIOD = os.environ.get("MIN_PERIOD", "2024-03-01")

# Verified against ONS Code History Database (June 2026), Changes.csv:
# The Barnsley and Sheffield (Boundary Changes) Order 2024 (SI 1328/2024),
# operative 2025-04-01. PIPR publishes the whole back series on the new codes;
# the pipeline's canonical key is LAD24CD, so map back to the predecessors.
# These two codes are NOT in la_code_lookup (which is left untouched by design).
CHD_SUPPLEMENT = {"E08000038": "E08000016", "E08000039": "E08000019"}

BLOCKS = [
    ("",                   "all",           "all"),
    (" one bed",           "bedroom",       "1_bed"),
    (" two bed",           "bedroom",       "2_bed"),
    (" three bed",         "bedroom",       "3_bed"),
    (" four or more bed",  "bedroom",       "4_plus_bed"),
    (" detached",          "property_type", "detached"),
    (" semidetached",      "property_type", "semi_detached"),
    (" terraced",          "property_type", "terraced"),
    (" flat maisonette",   "property_type", "flat_maisonette"),
]


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
    src = ROOT / "data" / "raw" / f"pipr_{EDITION}.xlsx"
    df = pd.read_excel(src, sheet_name="Table 1", header=2)
    print(f"read {src.name}: {df.shape[0]} rows")

    df["Area code"] = df["Area code"].astype(str)
    eng = df[df["Area code"].str[:3].isin(["E06", "E07", "E08", "E09"])].copy()
    eng["period"] = pd.to_datetime(eng["Time period"])
    print(f"England LA rows: {len(eng)} ({eng['Area code'].nunique()} distinct codes)")

    latest_period = eng["period"].max()
    eng = eng[eng["period"] >= MIN_PERIOD]
    print(f"rows with period >= {MIN_PERIOD}: {len(eng)}; "
          f"latest period {latest_period.date()} (provisional)")

    # --- resolve codes: la_code_lookup first, CHD supplement second ---
    conn = pg_conn()
    cur = conn.cursor()
    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    lookup = dict(cur.fetchall())
    conn.close()

    src_codes = sorted(eng["Area code"].unique())
    resolved, via_supplement, unresolved = {}, [], []
    for c in src_codes:
        if c in lookup:
            resolved[c] = lookup[c]
        elif c in CHD_SUPPLEMENT:
            resolved[c] = CHD_SUPPLEMENT[c]
            via_supplement.append(c)
        else:
            unresolved.append(c)

    print(f"codes resolved via la_code_lookup: {len(resolved) - len(via_supplement)}")
    print(f"codes resolved via CHD supplement (SI 1328/2024): {via_supplement}")
    print(f"codes UNRESOLVED: {unresolved if unresolved else 0}")
    if unresolved:
        n_drop = len(eng[eng["Area code"].isin(unresolved)])
        print(f"rows dropped for unresolved codes: {n_drop}")
        eng = eng[~eng["Area code"].isin(unresolved)]
    eng["lad24cd"] = eng["Area code"].map(resolved)

    # --- reshape wide -> long ---
    frames = []
    for suffix, btype, cat in BLOCKS:
        sub = pd.DataFrame({
            "lad24cd": eng["lad24cd"],
            "period": eng["period"].dt.date,
            "breakdown_type": btype,
            "category": cat,
            "mean_rent": pd.to_numeric(eng[f"Rental price{suffix}"], errors="coerce"),
            "rent_index": pd.to_numeric(eng[f"Index{suffix}"], errors="coerce"),
            "annual_pct_change": pd.to_numeric(eng[f"Annual change{suffix}"],
                                               errors="coerce"),
        })
        frames.append(sub)
    long = pd.concat(frames, ignore_index=True)
    for col in ("mean_rent", "rent_index", "annual_pct_change"):
        long[col] = long[col].round(2)
    long["provisional"] = long["period"] == latest_period.date()

    # [x]/[z] markers coerce to NaN; count them so nothing is silently lost
    n_null = long[["mean_rent", "rent_index", "annual_pct_change"]].isna().sum()
    print(f"null value counts (markers/blanks): {n_null.to_dict()}")

    dup = long.duplicated(["lad24cd", "period", "breakdown_type", "category"]).sum()
    if dup:
        print(f"ERROR: {dup} duplicate keys after transform", file=sys.stderr)
        sys.exit(1)

    out = ROOT / "data" / "processed" / f"la_private_rents_{EDITION}.csv"
    long.to_csv(out, index=False)
    print(f"wrote {out} ({len(long)} rows, "
          f"{long['lad24cd'].nunique()} LAs, {long['period'].nunique()} periods)")


if __name__ == "__main__":
    main()
