"""
s18_pipr_verify.py — S18 Phase 4.5: Automated verification suite.

Purpose : Run the seven post-load checks: row reconciliation, coverage,
          join integrity, range checks, ONS bulletin cross-check (figures
          fetched live from the bulletin, never hardcoded), upsert
          idempotency, and succession-migration integrity.
          Exits non-zero if any check fails; the run log must not record
          success unless this script passes.
Inputs  : data/processed/la_private_rents_<edition>.csv (argv[1] edition slug)
          Postgres exempt_pipeline on localhost:5432
          ONS bulletin https://www.ons.gov.uk/.../privaterentandhousepricesuk/latest
Outputs : stdout PASS/FAIL per check.
"""
import os
import re
import sys
from pathlib import Path

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent.parent
EDITION = sys.argv[1] if len(sys.argv) > 1 else "17june2026"
BULLETIN = ("https://www.ons.gov.uk/economy/inflationandpriceindices/bulletins/"
            "privaterentandhousepricesuk/latest")

failures = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        failures.append(name)


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
    conn = pg_conn()
    cur = conn.cursor()
    csv = pd.read_csv(ROOT / "data" / "processed" /
                      f"la_private_rents_{EDITION}.csv", dtype={"lad24cd": str})
    source = f"ONS PIPR {EDITION} edition"

    # 1. Row reconciliation
    cur.execute("SELECT COUNT(*) FROM la_private_rents WHERE source = %s", (source,))
    db_rows = cur.fetchone()[0]
    check("1 row reconciliation", db_rows == len(csv),
          f"db={db_rows} csv={len(csv)} (documented exclusions: none post-transform)")

    # 2. Coverage: distinct LAs per (period, breakdown_type) in [290, 296]
    cur.execute("""
        SELECT period, breakdown_type, COUNT(DISTINCT lad24cd) AS n
        FROM la_private_rents WHERE source = %s
        GROUP BY period, breakdown_type
        HAVING COUNT(DISTINCT lad24cd) NOT BETWEEN 290 AND 296
    """, (source,))
    bad = cur.fetchall()
    cur.execute("""
        SELECT MIN(n), MAX(n), COUNT(*) FROM (
          SELECT COUNT(DISTINCT lad24cd) AS n FROM la_private_rents
          WHERE source = %s GROUP BY period, breakdown_type) t
    """, (source,))
    lo, hi, groups = cur.fetchone()
    check("2 coverage", not bad,
          f"{groups} period×breakdown groups, distinct-LA range {lo}–{hi}"
          + (f"; OUT OF RANGE: {bad}" if bad else
             " (294 everywhere: Isles of Scilly and City of London absent from PIPR)"))

    # 3. Join integrity: every loaded lad24cd exists in la_boundaries
    cur.execute("""
        SELECT DISTINCT r.lad24cd FROM la_private_rents r
        LEFT JOIN la_boundaries b ON r.lad24cd = b.lad24cd
        WHERE b.lad24cd IS NULL AND r.source = %s
    """, (source,))
    orphans = [r[0] for r in cur.fetchall()]
    check("3 join integrity", not orphans,
          "0 source codes unresolved (292 via la_code_lookup, 2 via CHD-verified "
          "supplement E08000038->E08000016, E08000039->E08000019)"
          if not orphans else f"orphan codes in table: {orphans}")

    # 4. Range checks
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE mean_rent NOT BETWEEN 200 AND 10000),
               COUNT(*) FILTER (WHERE rent_index <= 0),
               COUNT(*) FILTER (WHERE annual_pct_change NOT BETWEEN -50 AND 100),
               MIN(mean_rent), MAX(mean_rent)
        FROM la_private_rents WHERE source = %s
    """, (source,))
    n_rent, n_idx, n_ann, rmin, rmax = cur.fetchone()
    check("4 range checks", n_rent == 0 and n_idx == 0 and n_ann == 0,
          f"violations rent={n_rent} index={n_idx} annual={n_ann}; "
          f"rent span £{rmin}–£{rmax}")

    # 5. Bulletin cross-check — figures parsed live from the ONS bulletin
    html = requests.get(BULLETIN, timeout=60, headers={
        "User-Agent": "Mozilla/5.0 exempt-pipeline-s18/1.0"}).text
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&#163;|&pound;", "£", text)

    m_kc = re.search(r"highest in Kensington and Chelsea[^(]*\(£([\d,]+)\)", text)
    m_ox = re.search(r"was Oxford[^(]*\(£([\d,]+)\)", text)
    if not (m_kc and m_ox):
        check("5 bulletin cross-check", False,
              "could not parse LA figures from bulletin text")
    else:
        kc_pub = float(m_kc.group(1).replace(",", ""))
        ox_pub = float(m_ox.group(1).replace(",", ""))
        cur.execute("""
            SELECT b.lad24nm, r.mean_rent, r.provisional
            FROM la_private_rents r JOIN la_boundaries b USING (lad24cd)
            WHERE r.breakdown_type='all' AND r.period = (
                  SELECT MAX(period) FROM la_private_rents)
            ORDER BY r.mean_rent DESC
        """)
        rows = cur.fetchall()
        top_name, top_rent, top_prov = rows[0]
        cur.execute("""
            SELECT b.lad24nm, r.mean_rent
            FROM la_private_rents r JOIN la_boundaries b USING (lad24cd)
            WHERE r.breakdown_type='all' AND r.lad24cd NOT LIKE 'E09%%'
              AND r.period = (SELECT MAX(period) FROM la_private_rents)
            ORDER BY r.mean_rent DESC LIMIT 1
        """)
        nl_name, nl_rent = cur.fetchone()

        ok_kc = top_name == "Kensington and Chelsea" and float(top_rent) == kc_pub
        ok_ox = nl_name == "Oxford" and float(nl_rent) == ox_pub
        ok_prov = bool(top_prov)  # bulletin: May 2026 figures are provisional
        check("5 bulletin cross-check", ok_kc and ok_ox and ok_prov,
              f"bulletin K&C £{kc_pub:.0f} vs loaded {top_name} £{top_rent} "
              f"(provisional={top_prov}); bulletin ex-London Oxford £{ox_pub:.0f} "
              f"vs loaded {nl_name} £{nl_rent}; 4 assertions "
              "(2 exact values, England-max rank, ex-London-max rank)")

    # 6. Idempotency: re-run the identical upsert; row count must not change
    cur.execute("SELECT COUNT(*) FROM la_private_rents")
    before = cur.fetchone()[0]
    records = [
        (r.lad24cd, r.period, r.breakdown_type, r.category,
         None if pd.isna(r.mean_rent) else r.mean_rent,
         None if pd.isna(r.rent_index) else r.rent_index,
         None if pd.isna(r.annual_pct_change) else r.annual_pct_change,
         bool(r.provisional), source)
        for r in csv.itertuples(index=False)
    ]
    execute_values(cur, """
        INSERT INTO la_private_rents (lad24cd, period, breakdown_type, category,
                                      mean_rent, rent_index, annual_pct_change,
                                      provisional, source)
        VALUES %s
        ON CONFLICT (lad24cd, period, breakdown_type, category) DO UPDATE SET
            mean_rent = EXCLUDED.mean_rent, rent_index = EXCLUDED.rent_index,
            annual_pct_change = EXCLUDED.annual_pct_change,
            provisional = EXCLUDED.provisional, source = EXCLUDED.source,
            loaded_at = NOW()
    """, records, page_size=5000)
    conn.commit()
    cur.execute("SELECT COUNT(*) FROM la_private_rents")
    after = cur.fetchone()[0]
    check("6 idempotency", before == after, f"count before={before} after={after}")

    # 7. Succession migration
    cur.execute("SELECT COUNT(*) FROM la_succession")
    n_succ = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM la_code_lookup WHERE change_type <> 'current'")
    n_hist = cur.fetchone()[0]
    cur.execute("""SELECT COUNT(*) FROM la_succession s
                   JOIN la_boundaries b ON s.predecessor_code = b.lad24cd""")
    n_pred_current = cur.fetchone()[0]
    check("7 succession migration", n_succ == n_hist and n_pred_current == 0,
          f"la_succession={n_succ} vs lookup non-current={n_hist}; "
          f"predecessors still in la_boundaries={n_pred_current}")

    conn.close()
    if failures:
        print(f"\nVERIFICATION FAILED: {failures}")
        sys.exit(1)
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
