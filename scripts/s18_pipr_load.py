"""
s18_pipr_load.py — S18 Phase 4.1–4.4: Create tables and load Postgres.

Purpose : Idempotently create la_private_rents, la_geography, la_succession
          (owner pipeline_user, matching the other pipeline tables); seed
          la_succession from la_code_lookup's historical rows; seed
          la_geography from the ONS Code History Database (June 2026)
          ChangeHistory.csv; batch-upsert the processed rent CSV.
Inputs  : data/processed/la_private_rents_<edition>.csv (argv[1] edition slug)
          data/raw/chd_june2026.zip (ChangeHistory.csv inside)
          Postgres exempt_pipeline on localhost:5432
Outputs : Three tables created/updated in exempt_pipeline; row counts on stdout.
"""
import os
import sys
import zipfile
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent.parent
EDITION = sys.argv[1] if len(sys.argv) > 1 else "17june2026"
RUN_DATE = "2026-07-11"

DDL = """
CREATE TABLE IF NOT EXISTS la_private_rents (
    lad24cd           VARCHAR(9)   NOT NULL,
    period            DATE         NOT NULL,
    breakdown_type    VARCHAR(20)  NOT NULL,
    category          VARCHAR(30)  NOT NULL,
    mean_rent         NUMERIC(8,2),
    rent_index        NUMERIC(8,2),
    annual_pct_change NUMERIC(6,2),
    provisional       BOOLEAN      DEFAULT FALSE,
    source            TEXT,
    loaded_at         TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (lad24cd, period, breakdown_type, category)
);

COMMENT ON TABLE la_private_rents IS
'ONS Price Index of Private Rents (PIPR), LA-level rent levels and indices, England. CAVEAT 1: tenancies in receipt of housing benefit are excluded by ONS where identifiable — figures represent open-market opportunity cost, not HB-supported rents. CAVEAT 2: PIPR is stock-based (new and existing tenancies blended) — it lags the price of a newly agreed lease in a rising market. Reference period for the index is January 2023 = 100. Latest month is provisional and revised in the following edition.';

COMMENT ON COLUMN la_private_rents.provisional IS
'TRUE for values published as provisional (typically the latest period); overwritten to FALSE when a later edition finalises them.';

CREATE TABLE IF NOT EXISTS la_geography (
    gss_code     VARCHAR(9)   NOT NULL,
    la_name      VARCHAR(100),
    boundary_set VARCHAR(10)  NOT NULL,
    valid_from   DATE         NOT NULL,
    valid_to     DATE,
    loaded_at    TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (gss_code, valid_from)
);

COMMENT ON TABLE la_geography IS
'Geography dimension with code validity windows. Built ahead of LGR: East/West Surrey vest 1 April 2027; most remaining new unitaries vest 1 April 2028. valid_to NULL = current. Successor mappings live in la_succession.';

CREATE TABLE IF NOT EXISTS la_succession (
    predecessor_code VARCHAR(9)   NOT NULL,
    successor_code   VARCHAR(9)   NOT NULL,
    change_date      DATE         NOT NULL,
    change_type      VARCHAR(50),
    apportionment    NUMERIC(6,5) DEFAULT 1.0,
    source           TEXT,
    loaded_at        TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (predecessor_code, successor_code, change_date)
);

COMMENT ON TABLE la_succession IS
'Successor mapping supporting one-to-many splits with population apportionment. Supersedes the one-to-one assumption in la_code_lookup for the 2027/2028 LGR wave, where some districts split between new unitaries. apportionment = share of the predecessor assigned to the successor (1.0 for whole-area transfers).';
"""


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
    conn.autocommit = False
    cur = conn.cursor()

    # 4.1 tables + ownership (pipeline pattern: n8n credential runs as pipeline_user)
    cur.execute(DDL)
    for t in ("la_private_rents", "la_geography", "la_succession"):
        cur.execute(f"ALTER TABLE {t} OWNER TO pipeline_user")
    print("tables created / ownership set to pipeline_user")

    # 4.2 seed la_succession from la_code_lookup historical rows
    cur.execute("""
        INSERT INTO la_succession (predecessor_code, successor_code, change_date,
                                   change_type, apportionment, source)
        SELECT old_code, new_code, effective_date, change_type, 1.0,
               'migrated from la_code_lookup ' || %s
        FROM la_code_lookup
        WHERE change_type <> 'current'
        ON CONFLICT DO NOTHING
    """, (RUN_DATE,))
    cur.execute("SELECT COUNT(*) FROM la_succession")
    print(f"la_succession rows: {cur.fetchone()[0]}")

    # 4.3 seed la_geography from CHD ChangeHistory (authoritative start dates)
    z = zipfile.ZipFile(ROOT / "data" / "raw" / "chd_june2026.zip")
    hist = pd.read_csv(z.open("ChangeHistory.csv"), dtype=str,
                       encoding="latin-1", low_memory=False)
    hist = hist[hist["ENTITYCD"].isin(["E06", "E07", "E08", "E09"])]

    cur.execute("SELECT lad24cd, lad24nm FROM la_boundaries")
    boundaries = cur.fetchall()

    geo_rows, missing_chd = [], []
    for code, name in boundaries:
        rows = hist[hist["GEOGCD"] == code]
        if rows.empty:
            missing_chd.append(code)
            continue
        oper = pd.to_datetime(rows["OPER_DATE"], dayfirst=True).min().date()
        term_raw = rows.sort_values("OPER_DATE").iloc[-1]["TERM_DATE"]
        term = (pd.to_datetime(term_raw, dayfirst=True).date()
                if pd.notna(term_raw) else None)
        geo_rows.append((code, name, "LAD24", oper, term))

    if missing_chd:
        print(f"ERROR: {len(missing_chd)} LAD24 codes not in CHD: {missing_chd}",
              file=sys.stderr)
        conn.rollback()
        sys.exit(1)

    execute_values(cur, """
        INSERT INTO la_geography (gss_code, la_name, boundary_set, valid_from, valid_to)
        VALUES %s
        ON CONFLICT (gss_code, valid_from) DO UPDATE SET
            la_name = EXCLUDED.la_name, valid_to = EXCLUDED.valid_to,
            loaded_at = NOW()
    """, geo_rows)
    cur.execute("SELECT COUNT(*), COUNT(valid_to) FROM la_geography")
    n, n_term = cur.fetchone()
    print(f"la_geography rows: {n} ({n_term} with a valid_to termination date)")

    # 4.4 upsert rent data
    csv_path = ROOT / "data" / "processed" / f"la_private_rents_{EDITION}.csv"
    df = pd.read_csv(csv_path, dtype={"lad24cd": str})
    source = f"ONS PIPR {EDITION} edition"
    records = [
        (r.lad24cd, r.period, r.breakdown_type, r.category,
         None if pd.isna(r.mean_rent) else r.mean_rent,
         None if pd.isna(r.rent_index) else r.rent_index,
         None if pd.isna(r.annual_pct_change) else r.annual_pct_change,
         bool(r.provisional), source)
        for r in df.itertuples(index=False)
    ]
    execute_values(cur, """
        INSERT INTO la_private_rents (lad24cd, period, breakdown_type, category,
                                      mean_rent, rent_index, annual_pct_change,
                                      provisional, source)
        VALUES %s
        ON CONFLICT (lad24cd, period, breakdown_type, category) DO UPDATE SET
            mean_rent         = EXCLUDED.mean_rent,
            rent_index        = EXCLUDED.rent_index,
            annual_pct_change = EXCLUDED.annual_pct_change,
            provisional       = EXCLUDED.provisional,
            source            = EXCLUDED.source,
            loaded_at         = NOW()
    """, records, page_size=5000)
    cur.execute("SELECT COUNT(*) FROM la_private_rents")
    print(f"la_private_rents rows: {cur.fetchone()[0]} (upserted {len(records)})")

    conn.commit()
    conn.close()
    print("COMMITTED")


if __name__ == "__main__":
    main()
