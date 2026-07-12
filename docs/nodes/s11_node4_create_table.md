# Node 4 - Create cqc_locations

## Type
Postgres DDL, one operation (`scripts/s11_cqc_load.py`, node 4 step)

## Purpose
Idempotently create the `cqc_locations` table in `exempt_pipeline` and set ownership to `pipeline_user`, the account n8n's Postgres credential runs as (the S14/S18 rule: tables created as `n8nuser` throw permission errors inside workflows).

## Query
```sql
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
ALTER TABLE cqc_locations OWNER TO pipeline_user;
```
Table and column comments (dual-lens usage, CQC migration caveat, mapping_method values, is_active semantics) are in the script's DDL block.

## Behaviour
`CREATE TABLE IF NOT EXISTS` plus an ownership statement; running it against an existing table changes nothing but ownership, which is already correct.

## Connection
- Input: Node 3 - Resolve LAD24CD
- Output: Node 5 - Upsert locations

## Verified Output (2026-07-12)
Table created in exempt_pipeline, owner pipeline_user, 40 columns.
