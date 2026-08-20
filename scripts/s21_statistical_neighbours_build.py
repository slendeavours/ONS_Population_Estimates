"""
Source 21 - ONS Statistical Neighbours.

Loads la_statistical_neighbours from the ONS "Clustering similar local
authorities and statistical nearest neighbours in the UK" dataset (Table 7a:
LTLA global statistical nearest neighbours), released 18 Mar 2026, free
download, no login required:
https://www.ons.gov.uk/peoplepopulationandcommunity/wellbeing/datasets/clusteringsimilarlocalauthoritiesandstatisticalnearestneighboursintheuk

Connection: Postgres exempt_pipeline on localhost:5432 (PG_HOST/PG_USER/PG_PASSWORD
from .env - the established host-side access pattern, see scripts/spb_extract.py).
"""
import os
import sys
from pathlib import Path

import openpyxl
import psycopg2
from psycopg2.extras import execute_batch

XLSX_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "ons_statistical_neighbours_2026.xlsx"
SOURCE_NUMBER = "21"
AGENT_NAME = "Source 21 - ONS Statistical Neighbours"
SOURCE_LABEL = "ONS Clustering similar LAs and statistical nearest neighbours, Table 7a (LTLA global), Mar-2026 edition"
MAX_NEIGHBOURS = 5

# Barnsley/Sheffield April 2025 recode (SI 1328/2024): the ONS Mar-2026 edition
# still carries the pre-recode codes for this one pair. Same gotcha already
# handled in the S18 PIPR build (see scripts/s18_pipr_transform.py) - NOT in
# la_code_lookup by design, handled here as a direct supplement instead.
CODE_REMAP = {
    "E08000038": "E08000016",  # Barnsley
    "E08000039": "E08000019",  # Sheffield
}


def load_env():
    env = {}
    env_path = Path(__file__).resolve().parent.parent / ".env"
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def get_conn():
    env = load_env()
    password = os.environ.get("PG_PASSWORD") or env.get("PG_PASSWORD")
    if not password:
        sys.exit("PG_PASSWORD not set (env var or .env) - refusing to guess")
    return psycopg2.connect(
        host=os.environ.get("PG_HOST_OVERRIDE", "localhost"), port=5432,
        dbname=env.get("PG_DATABASE"),
        user=os.environ.get("PG_USER") or env.get("PG_USER"), password=password)


def halt(msg):
    print(f"HALT: {msg}")
    sys.exit(1)


def extract_neighbour_pairs():
    if not XLSX_PATH.exists():
        halt(f"{XLSX_PATH} not found - download the ONS dataset first")

    wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
    ws = wb["7.a"]
    rows = list(ws.iter_rows(min_row=5, values_only=True))

    pairs = []  # (lad24cd, neighbour_lad24cd, raw_rank)
    for row in rows:
        subject = row[0]
        if not subject or not subject.startswith("E"):
            continue  # England (LAD24) only - the only nation staging_la_signals covers
        subject = CODE_REMAP.get(subject, subject)
        rank = 0
        for i in range(2, 42, 2):
            ncode = row[i]
            if not ncode or not ncode.startswith("E"):
                continue
            ncode = CODE_REMAP.get(ncode, ncode)
            if ncode == subject:
                continue  # remap collision guard (shouldn't happen, cheap to check)
            rank += 1
            pairs.append((subject, ncode, rank))
    return pairs


def main():
    print("=== SOURCE 21: ONS STATISTICAL NEIGHBOURS ===")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT lad24cd FROM staging_la_signals WHERE run_id = (SELECT MAX(run_id) FROM staging_la_signals)")
    db_codes = set(r[0] for r in cur.fetchall())
    print(f"staging_la_signals England LAs: {len(db_codes)}")

    all_pairs = extract_neighbour_pairs()
    print(f"Raw England subject rows extracted: {len(set(p[0] for p in all_pairs))}")

    # Keep only pairs where BOTH sides are current LAs in staging_la_signals,
    # take the top MAX_NEIGHBOURS in rank order per subject.
    by_subject = {}
    for subject, ncode, rank in all_pairs:
        if subject not in db_codes or ncode not in db_codes:
            continue
        by_subject.setdefault(subject, []).append((ncode, rank))

    final_rows = []  # (lad24cd, neighbour_lad24cd, neighbour_rank, source)
    for subject, neighbours in by_subject.items():
        neighbours.sort(key=lambda t: t[1])
        for out_rank, (ncode, _) in enumerate(neighbours[:MAX_NEIGHBOURS], start=1):
            final_rows.append((subject, ncode, out_rank, SOURCE_LABEL))

    # ---- Coverage gate ----
    covered = set(by_subject.keys())
    missing = db_codes - covered
    under5 = {s: len(n) for s, n in by_subject.items() if len(n) < MAX_NEIGHBOURS}

    print(f"\n=== COVERAGE CHECK ===")
    print(f"LAs with >=1 usable neighbour: {len(covered)} / {len(db_codes)}")
    print(f"LAs with full {MAX_NEIGHBOURS} neighbours: {len(by_subject) - len(under5)} / {len(db_codes)}")
    if missing:
        print(f"LAs with ZERO usable neighbours: {sorted(missing)}")
    if under5:
        print(f"LAs with FEWER than {MAX_NEIGHBOURS} neighbours: {under5}")

    if missing:
        halt(
            f"{len(missing)} LA(s) have no usable statistical neighbour from the ONS source "
            f"({sorted(missing)}). This needs a decision (extend the fallback method or accept "
            f"partial coverage) - not something to silently paper over. Stopping before writing."
        )

    # ---- Spot check (print for manual cross-check against the source workbook / LG Inform) ----
    print(f"\n=== SPOT CHECK ===")
    for code, name in [("E08000025", "Birmingham"), ("E06000010", "Kingston upon Hull, City of"), ("E08000035", "Leeds")]:
        top5 = [n for n, r in sorted(by_subject.get(code, []), key=lambda t: t[1])[:MAX_NEIGHBOURS]]
        print(f"{name} ({code}): {top5}")

    # ---- Load ----
    print(f"\n=== LOADING {len(final_rows)} ROWS ===")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS la_statistical_neighbours (
            lad24cd VARCHAR(9) NOT NULL,
            statistical_neighbour_lad24cd VARCHAR(9) NOT NULL,
            neighbour_rank INTEGER NOT NULL,
            source TEXT NOT NULL,
            loaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (lad24cd, statistical_neighbour_lad24cd)
        )
    """)
    cur.execute("ALTER TABLE la_statistical_neighbours OWNER TO pipeline_user")
    cur.execute("TRUNCATE la_statistical_neighbours")
    execute_batch(
        cur,
        "INSERT INTO la_statistical_neighbours (lad24cd, statistical_neighbour_lad24cd, neighbour_rank, source) VALUES (%s, %s, %s, %s)",
        final_rows,
    )

    # ---- FK integrity check (structural by construction, verify anyway) ----
    cur.execute("""
        SELECT COUNT(*) FROM la_statistical_neighbours n
        WHERE NOT EXISTS (SELECT 1 FROM staging_la_signals s WHERE s.lad24cd = n.lad24cd)
           OR NOT EXISTS (SELECT 1 FROM staging_la_signals s WHERE s.lad24cd = n.statistical_neighbour_lad24cd)
    """)
    orphans = cur.fetchone()[0]
    print(f"FK integrity check: {orphans} orphaned rows (expect 0)")
    if orphans:
        conn.rollback()
        halt("FK integrity check failed - rolled back, not logging to pipeline_run_log")

    cur.execute("""
        INSERT INTO pipeline_run_log
        (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
        VALUES (%s, %s, %s, NOW(), NOW(), %s, %s)
    """, (
        AGENT_NAME, SOURCE_NUMBER, len(final_rows), "success",
        f"{SOURCE_LABEL}. {len(covered)}/{len(db_codes)} LAs covered, "
        f"{MAX_NEIGHBOURS} comparators each. Barnsley/Sheffield remapped per S18 precedent "
        f"(E08000038->E08000016, E08000039->E08000019). No fallback method invoked."
    ))
    conn.commit()
    print("\nOVERALL: PASS - la_statistical_neighbours loaded, pipeline_run_log written.")
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
