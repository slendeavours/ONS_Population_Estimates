"""S15 — Land Registry UK HPI ETL: average house prices per English LA into Postgres."""

import os
import sys
import csv
import math
import datetime
import psycopg2
import psycopg2.extras

def _require_env(name):
    """Credentials must come from the environment. Never fall back to a
    literal: a default in source is a published credential."""
    value = os.environ.get(name)
    if not value:
        sys.exit(f"HARD STOP: {name} is not set. Set it in the environment "
                 f"or .env. This script will not guess a credential.")
    return value

# This script never loaded .env at all; it read os.environ only, so it worked
# from a shell that happened to have the variables exported and nowhere else.
from _db import get_conn  # noqa: E402

EDITION = "April 2026"
_TMP = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "s15_hpi")
FILE1 = os.path.join(_TMP, "avg_prices.csv")
FILE2 = os.path.join(_TMP, "avg_prices_property_type.csv")

ENGLISH_LA_PREFIXES = ("E06", "E07", "E08", "E09")
MIN_PERIOD = datetime.date(2022, 1, 1)

HARD_RECODES = {
    "E08000038": "E08000016",  # Barnsley
    "E08000039": "E08000019",  # Sheffield
}

DDL = """
CREATE TABLE IF NOT EXISTS la_house_prices (
    lad24cd            VARCHAR(9)      NOT NULL,
    period             DATE            NOT NULL,
    avg_price_all      NUMERIC(12,2),
    avg_price_all_sa   NUMERIC(12,2),
    annual_change_pct  NUMERIC(6,2),
    avg_price_detached NUMERIC(12,2),
    avg_price_semi     NUMERIC(12,2),
    avg_price_terraced NUMERIC(12,2),
    avg_price_flat     NUMERIC(12,2),
    loaded_at          TIMESTAMPTZ     DEFAULT NOW(),
    PRIMARY KEY (lad24cd, period)
);

COMMENT ON TABLE la_house_prices IS
  'Land Registry UK HPI average prices per English LA per month.
   Source: HM Land Registry / ONS UK HPI. Grain: lad24cd × period.
   avg_price_* values are NULL where the Land Registry suppresses
   due to low transaction volumes.
   First loaded: 2026-07-14.';
"""

UPSERT = """
INSERT INTO la_house_prices (
    lad24cd, period,
    avg_price_all, avg_price_all_sa, annual_change_pct,
    avg_price_detached, avg_price_semi,
    avg_price_terraced, avg_price_flat
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (lad24cd, period) DO UPDATE SET
    avg_price_all      = EXCLUDED.avg_price_all,
    avg_price_all_sa   = EXCLUDED.avg_price_all_sa,
    annual_change_pct  = EXCLUDED.annual_change_pct,
    avg_price_detached = EXCLUDED.avg_price_detached,
    avg_price_semi     = EXCLUDED.avg_price_semi,
    avg_price_terraced = EXCLUDED.avg_price_terraced,
    avg_price_flat     = EXCLUDED.avg_price_flat,
    loaded_at          = NOW();
"""


def safe_numeric(val):
    if val is None or val == "" or val == "NA":
        return None
    try:
        v = float(val)
        return None if math.isnan(v) or math.isinf(v) else v
    except (ValueError, TypeError):
        return None


def load_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    started_at = datetime.datetime.now(datetime.timezone.utc)

    conn = get_conn()
    conn.autocommit = False
    cur = conn.cursor()

    # --- Step 3: create table ---
    cur.execute(DDL)
    cur.execute("ALTER TABLE la_house_prices OWNER TO pipeline_user")
    conn.commit()
    print("Table la_house_prices created/verified (owner: pipeline_user).")

    # --- Load lookup tables for code reconciliation ---
    cur.execute("SELECT old_code, new_code FROM la_code_lookup")
    code_lookup = {r[0]: r[1] for r in cur.fetchall()}

    cur.execute("SELECT lad24cd FROM la_boundaries")
    valid_lads = {r[0] for r in cur.fetchall()}

    # Verify hard-recode targets exist
    for old, new in HARD_RECODES.items():
        assert new in valid_lads, f"Hard recode target {new} not in la_boundaries"
    print("Hard recode targets verified in la_boundaries.")

    # --- Step 4: process ---
    print("\nLoading File 1 (avg_prices)...")
    rows1 = load_csv(FILE1)
    print(f"  Raw rows: {len(rows1)}")

    print("Loading File 2 (avg_prices_property_type)...")
    rows2 = load_csv(FILE2)
    print(f"  Raw rows: {len(rows2)}")

    # Build File 2 lookup keyed by (Area_Code, Date)
    pt_lookup = {}
    for r in rows2:
        key = (r["Area_Code"], r["Date"])
        pt_lookup[key] = r

    # Filter and process File 1
    unresolved = {}
    merged_rows = []
    unmatched_f2 = 0

    for r in rows1:
        area_code = r["Area_Code"]
        if not area_code.startswith(ENGLISH_LA_PREFIXES):
            continue

        period = datetime.date.fromisoformat(r["Date"])
        if period < MIN_PERIOD:
            continue

        # Code reconciliation
        if area_code in HARD_RECODES:
            lad24cd = HARD_RECODES[area_code]
        elif area_code in valid_lads:
            lad24cd = area_code
        elif area_code in code_lookup:
            lad24cd = code_lookup[area_code]
        else:
            unresolved[area_code] = unresolved.get(area_code, 0) + 1
            continue

        # Merge with File 2
        pt = pt_lookup.get((r["Area_Code"], r["Date"]))
        if pt is None:
            unmatched_f2 += 1

        merged_rows.append((
            lad24cd,
            period,
            safe_numeric(r.get("Average_Price")),
            safe_numeric(r.get("Average_Price_SA")),
            safe_numeric(r.get("Annual_Change")),
            safe_numeric(pt["Detached_Average_Price"]) if pt else None,
            safe_numeric(pt["Semi_Detached_Average_Price"]) if pt else None,
            safe_numeric(pt["Terraced_Average_Price"]) if pt else None,
            safe_numeric(pt["Flat_Average_Price"]) if pt else None,
        ))

    print(f"\nFiltered+merged rows for upsert: {len(merged_rows)}")
    print(f"File 2 unmatched (no property-type data): {unmatched_f2}")

    if unresolved:
        print(f"\nUnresolved area codes ({len(unresolved)}):")
        for code, cnt in sorted(unresolved.items()):
            print(f"  {code}: {cnt} rows")
    else:
        print("No unresolved area codes.")

    # --- Upsert in batches of 500 ---
    batch_size = 500
    total = len(merged_rows)
    for i in range(0, total, batch_size):
        batch = merged_rows[i : i + batch_size]
        psycopg2.extras.execute_batch(cur, UPSERT, batch, page_size=batch_size)
    conn.commit()
    print(f"\nUpserted {total} rows in {math.ceil(total / batch_size)} batches.")

    # --- Step 5: verification suite ---
    print("\n" + "=" * 60)
    print("VERIFICATION SUITE")
    print("=" * 60)
    all_pass = True

    # CHECK 1 — Row count
    cur.execute("SELECT COUNT(*) FROM la_house_prices")
    row_count = cur.fetchone()[0]
    c1 = row_count > 7800
    print(f"\nCHECK 1 — Row count: {row_count} {'PASS' if c1 else 'FAIL'} (expected > 7,800)")
    all_pass &= c1

    # CHECK 2 — LAs represented (295 expected; E06000053 Isles of Scilly has no LR data)
    cur.execute("SELECT COUNT(DISTINCT lad24cd) FROM la_house_prices")
    la_count = cur.fetchone()[0]
    EXPECTED_MISSING = {"E06000053"}  # Isles of Scilly — no HPI data (too few transactions)
    cur.execute("""
        SELECT b.lad24cd FROM la_boundaries b
        LEFT JOIN (SELECT DISTINCT lad24cd FROM la_house_prices) h ON h.lad24cd = b.lad24cd
        WHERE h.lad24cd IS NULL
    """)
    missing = [r[0] for r in cur.fetchall()]
    unexpected_missing = [c for c in missing if c not in EXPECTED_MISSING]
    c2 = len(unexpected_missing) == 0
    print(f"CHECK 2 — Distinct LAs: {la_count} {'PASS' if c2 else 'FAIL'} (expected 295+; Isles of Scilly excluded by LR)")
    if missing:
        print(f"  Missing LAD24CDs: {missing} (expected: {sorted(EXPECTED_MISSING)})")
    if unexpected_missing:
        print(f"  UNEXPECTED missing: {unexpected_missing}")
    all_pass &= c2

    # CHECK 3 — Target market spot-check
    cur.execute("""
        SELECT lad24cd, period, avg_price_all
        FROM la_house_prices
        WHERE lad24cd IN ('E08000025','E08000012','E06000018','E08000003','E06000009')
        AND period = (SELECT MAX(period) FROM la_house_prices)
        ORDER BY lad24cd
    """)
    spot = cur.fetchall()
    c3 = len(spot) == 5 and all(r[2] is not None for r in spot)
    print(f"CHECK 3 — Target market spot-check: {'PASS' if c3 else 'FAIL'}")
    for r in spot:
        print(f"  {r[0]}  {r[1]}  £{r[2]:,.0f}")
    all_pass &= c3

    # CHECK 4 — No implausible prices
    cur.execute("""
        SELECT COUNT(*) FROM la_house_prices
        WHERE avg_price_all IS NOT NULL
        AND (avg_price_all < 50000 OR avg_price_all > 2000000)
    """)
    implausible = cur.fetchone()[0]
    c4 = implausible == 0
    print(f"CHECK 4 — Implausible prices: {implausible} {'PASS' if c4 else 'FAIL'}")
    if not c4:
        cur.execute("""
            SELECT lad24cd, period, avg_price_all FROM la_house_prices
            WHERE avg_price_all IS NOT NULL
            AND (avg_price_all < 50000 OR avg_price_all > 2000000)
            LIMIT 10
        """)
        for r in cur.fetchall():
            print(f"  {r[0]}  {r[1]}  £{r[2]:,.0f}")
    all_pass &= c4

    # CHECK 5 — Period coverage
    cur.execute("SELECT MIN(period), MAX(period), COUNT(DISTINCT period) FROM la_house_prices")
    min_p, max_p, period_count = cur.fetchone()
    today = datetime.date.today()
    c5 = min_p <= MIN_PERIOD and (today - max_p).days <= 120  # 4 months: HPI has ~2-month pub lag
    print(f"CHECK 5 — Period range: {min_p} to {max_p}, {period_count} periods {'PASS' if c5 else 'FAIL'}")
    all_pass &= c5

    # CHECK 6 — Barnsley and Sheffield check
    cur.execute("""
        SELECT lad24cd, COUNT(*) FROM la_house_prices
        WHERE lad24cd IN ('E08000016','E08000019')
        GROUP BY lad24cd
    """)
    bs_rows = {r[0]: r[1] for r in cur.fetchall()}
    cur.execute("""
        SELECT COUNT(*) FROM la_house_prices
        WHERE lad24cd IN ('E08000038','E08000039')
    """)
    bad_codes = cur.fetchone()[0]
    c6 = (
        "E08000016" in bs_rows
        and "E08000019" in bs_rows
        and bad_codes == 0
    )
    print(f"CHECK 6 — Barnsley E08000016: {bs_rows.get('E08000016', 0)} rows, "
          f"Sheffield E08000019: {bs_rows.get('E08000019', 0)} rows, "
          f"bad codes (E08000038/39): {bad_codes} {'PASS' if c6 else 'FAIL'}")
    all_pass &= c6

    print(f"\n{'=' * 60}")
    print(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")
    print(f"{'=' * 60}")

    if not all_pass:
        print("\nVerification FAILED — not proceeding to pipeline log.")
        cur.close()
        conn.close()
        sys.exit(1)

    # --- Step 6: Log to pipeline_run_log ---
    cur.execute("""
        INSERT INTO pipeline_run_log
            (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
        VALUES
            (%s, %s, %s, %s, NOW(), %s, %s)
    """, (
        "Source 15 - Land Registry UK HPI",
        15,
        total,
        started_at,
        "success",
        f"Land Registry UK HPI monthly average prices per English LA. Edition: {EDITION}. "
        f"Period range: {min_p} to {max_p}. "
        f"Recode applied: E08000038→E08000016, E08000039→E08000019.",
    ))
    conn.commit()
    print("\nPipeline run logged to pipeline_run_log.")

    cur.close()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
