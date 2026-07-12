"""
s11_cqc_load.py — S11 Nodes 4-7: Create table, upsert, deactivate, log run.

Purpose : Four discrete operations, one per node, in one transaction:
          Node 4  CREATE TABLE IF NOT EXISTS cqc_locations, owner pipeline_user
                  (n8n's credential runs as pipeline_user - the S14/S18 rule).
          Node 5  Parameterised upsert on location_id. A location reappearing
                  after deregistration is reactivated (is_active=true,
                  deregistered_seen_date cleared) - the register is the truth.
          Node 6  Deactivation sweep: rows whose source_file_date predates the
                  current file were absent from it; they get is_active=false
                  and a deregistered_seen_date stamped once (the WHERE guard
                  keeps the original date on later runs). Re-running the same
                  file touches nothing - idempotent.
          Node 7  Log the run to pipeline_run_log.
Inputs  : data/processed/cqc_locations_mapped.csv
          data/raw/s11_csv/FILE_DATE.txt (source file date)
Outputs : cqc_locations created/updated in exempt_pipeline; run logged.
"""
import os
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "processed" / "cqc_locations_mapped.csv"
FILE_DATE = (ROOT / "data" / "raw" / "s11_csv" / "FILE_DATE.txt").read_text().strip()

DDL = """
CREATE TABLE IF NOT EXISTS cqc_locations (
    location_id                         VARCHAR(20)  PRIMARY KEY,
    provider_id                         VARCHAR(20),
    provider_name                       TEXT,
    brand_name                          TEXT,
    location_name                       TEXT,
    postcode                            VARCHAR(10),
    latitude                            NUMERIC(9,6),
    longitude                           NUMERIC(9,6),
    lad24cd                             VARCHAR(9)   NOT NULL,
    region                              VARCHAR(30),
    la_name_cqc                         VARCHAR(60),
    mapping_method                      VARCHAR(30)  NOT NULL,
    supported_living                    BOOLEAN      NOT NULL,
    personal_care                       BOOLEAN      NOT NULL,
    care_home                           BOOLEAN      NOT NULL,
    care_homes_beds                     INTEGER,
    domiciliary_care                    BOOLEAN      NOT NULL,
    extra_care_housing                  BOOLEAN      NOT NULL,
    shared_lives                        BOOLEAN      NOT NULL,
    accommodation_nursing_personal_care BOOLEAN      NOT NULL,
    band_learning_disabilities_autism   BOOLEAN      NOT NULL,
    band_mental_health                  BOOLEAN      NOT NULL,
    band_younger_adults                 BOOLEAN      NOT NULL,
    band_older_people                   BOOLEAN      NOT NULL,
    band_dementia                       BOOLEAN      NOT NULL,
    band_substance_misuse               BOOLEAN      NOT NULL,
    band_physical_disability            BOOLEAN      NOT NULL,
    band_detained_mha                   BOOLEAN      NOT NULL,
    dormant                             BOOLEAN      NOT NULL,
    dual_registered                     BOOLEAN      NOT NULL,
    dual_primary_id                     VARCHAR(20),
    latest_overall_rating               VARCHAR(40),
    rating_publication_date             DATE,
    inherited_rating                    BOOLEAN,
    inspection_directorate              VARCHAR(40),
    primary_inspection_category         VARCHAR(60),
    location_hsca_start_date            DATE,
    is_active                           BOOLEAN      NOT NULL DEFAULT TRUE,
    deregistered_seen_date              DATE,
    source_file_date                    DATE         NOT NULL,
    loaded_at                           TIMESTAMPTZ  DEFAULT NOW()
);

COMMENT ON TABLE cqc_locations IS
'CQC Care directory with filters - active Adult social care directorate locations, the pipeline''s only supply-side source. Dual lens: HSS reads supported_living and personal_care directly; UCWS reads the table as provider-landscape context. Dual-registered pairs appear twice - dedupe on dual_primary_id for provision counts, per CQC guidance. CAVEAT: CQC is migrating its directory to a new digital system (their README, July 2026) - deregistrations can appear late, and multi-service locations (homecare plus supported living, typically) now show Not Rated at location level. The Local Authority name column is upper-tier vintage-unknown; lad24cd comes from the spatial mapping, never from that name.';

COMMENT ON COLUMN cqc_locations.mapping_method IS
'How lad24cd was resolved: point_in_polygon (lat/long inside a la_boundaries polygon), nearest_fallback (coastal point outside all polygons, nearest assigned), postcode_api_fallback (no coordinates - postcodes.io lookup, code validated against la_boundaries).';

COMMENT ON COLUMN cqc_locations.is_active IS
'FALSE when the location stopped appearing in the monthly file (deregistration signal - see deregistered_seen_date). Reset to TRUE if it reappears.';
"""

UPSERT_COLS = [
    "location_id", "provider_id", "provider_name", "brand_name",
    "location_name", "postcode", "latitude", "longitude", "lad24cd", "region",
    "la_name_cqc", "mapping_method", "supported_living", "personal_care",
    "care_home", "care_homes_beds", "domiciliary_care", "extra_care_housing",
    "shared_lives", "accommodation_nursing_personal_care",
    "band_learning_disabilities_autism", "band_mental_health",
    "band_younger_adults", "band_older_people", "band_dementia",
    "band_substance_misuse", "band_physical_disability", "band_detained_mha",
    "dormant", "dual_registered", "dual_primary_id", "latest_overall_rating",
    "rating_publication_date", "inherited_rating", "inspection_directorate",
    "primary_inspection_category", "location_hsca_start_date",
    "source_file_date",
]


def pg_conn():
    """Connect using PG_* values from the repo .env (never hardcoded)."""
    env = {}
    env_file = ROOT / ".env"
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
    started = datetime.now(timezone.utc)
    df = pd.read_csv(SRC, dtype={"location_id": str, "provider_id": str,
                                 "dual_primary_id": str, "postcode": str})
    df["source_file_date"] = FILE_DATE
    bools = [c for c in UPSERT_COLS
             if c.startswith(("band_", "supported", "personal", "care_home",
                              "domiciliary", "extra_care", "shared_lives",
                              "accommodation", "dormant", "dual_registered"))
             and c != "care_homes_beds"]
    conn = pg_conn()
    conn.autocommit = False
    cur = conn.cursor()

    # Node 4 - create table + ownership
    cur.execute(DDL)
    cur.execute("ALTER TABLE cqc_locations OWNER TO pipeline_user")
    print("node4: table created / ownership set to pipeline_user")

    # Node 5 - upsert
    def cell(r, c):
        v = r[c]
        if pd.isna(v):
            return None
        if c in bools:
            return bool(v)
        if c == "inherited_rating":
            return bool(v)
        if c == "care_homes_beds":
            return int(v)
        return v

    records = [tuple(cell(r, c) for c in UPSERT_COLS)
               for _, r in df.iterrows()]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPSERT_COLS[1:])
    execute_values(cur, f"""
        INSERT INTO cqc_locations ({", ".join(UPSERT_COLS)})
        VALUES %s
        ON CONFLICT (location_id) DO UPDATE SET
            {set_clause},
            is_active = TRUE,
            deregistered_seen_date = NULL,
            loaded_at = NOW()
    """, records, page_size=2000)
    print(f"node5: upserted {len(records)} rows")

    # Node 6 - deactivation sweep (absent rows keep their old source_file_date)
    cur.execute("""
        UPDATE cqc_locations
        SET is_active = FALSE, deregistered_seen_date = %s
        WHERE is_active = TRUE AND source_file_date < %s
    """, (FILE_DATE, FILE_DATE))
    print(f"node6: deactivated {cur.rowcount} rows absent from {FILE_DATE} file")

    # Node 7 - run log
    completed = datetime.now(timezone.utc)
    cur.execute("""
        INSERT INTO pipeline_run_log (run_id, agent_name, source_number, status,
                                      rows_written, started_at, completed_at,
                                      duration_ms, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (str(uuid.uuid4()), "Source 11 - CQC Care Providers", "11", "success",
          len(records), started, completed,
          int((completed - started).total_seconds() * 1000),
          f"CQC Care directory with filters, file date {FILE_DATE}; "
          f"ASC directorate scope; upsert with deactivation"))
    print("node7: run logged")

    conn.commit()
    cur.execute("SELECT COUNT(*), COUNT(*) FILTER (WHERE is_active) "
                "FROM cqc_locations")
    total, active = cur.fetchone()
    print(f"cqc_locations: {total} rows, {active} active")
    conn.close()
    print("COMMITTED")


if __name__ == "__main__":
    main()
