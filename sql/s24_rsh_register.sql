-- S24 - RSH register of providers and regulatory judgements
--
-- HISTORICAL DDL. Generated from scripts/historical/s24_rsh_register_build.py on
-- 2026-08-14, which is retired (its load ran twice, run-log ids 96 and 101).
-- The three live tables below are now the latest-edition layers of
-- rsh_registered_providers_editions, rsh_regulatory_judgements_editions and
-- rsh_enforcement_notices_editions (append-only, each with a file-check
-- ledger <editions table>_file_checks); all are created and kept by
-- scripts/s24_rsh_register_editions.py (ddl). The live tables keep these
-- names, keys and columns. The register keeps every snapshot; the judgements
-- and notices tables hold the newest edition only. This file is a readable
-- copy of the original definition, not what to run.

CREATE TABLE IF NOT EXISTS rsh_registered_providers (
    snapshot_date       date        NOT NULL,
    registration_number text        NOT NULL,
    organisation_name   text        NOT NULL,
    registration_date   date,
    designation         text,
    corporate_form      text,
    notes               text,
    source_url          text        NOT NULL,
    source_file         text        NOT NULL,
    release_page_url    text        NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_date, registration_number)
);

CREATE TABLE IF NOT EXISTS rsh_regulatory_judgements (
    registration_number   text        NOT NULL,
    publication_date      date        NOT NULL,
    landlord_name         text        NOT NULL,
    landlord_type         text,
    status                text,
    consumer_grade        text,
    consumer_grade_change text,
    consumer_grade_date   date,
    governance_grade      text,
    governance_grade_change text,
    governance_grade_date date,
    viability_grade       text,
    viability_grade_change text,
    viability_grade_date  date,
    rent_grade            text,
    rent_grade_change     text,
    rent_grade_date       date,
    publication_type      text,
    engagement_process    text,
    name_or_code_change   text,
    other_landlords       text,
    edition_date          date        NOT NULL,
    source_url            text        NOT NULL,
    source_file           text        NOT NULL,
    release_page_url      text        NOT NULL,
    loaded_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (registration_number, publication_date)
);

CREATE TABLE IF NOT EXISTS rsh_enforcement_notices (
    registration_number  text        NOT NULL,
    publication_date     date        NOT NULL,
    provider_name        text        NOT NULL,
    status               text,
    publication_type     text,
    route                text,
    explanation          text,
    notice_date          date,
    name_or_code_change  text,
    other_providers      text,
    edition_date         date        NOT NULL,
    source_url           text        NOT NULL,
    source_file          text        NOT NULL,
    release_page_url     text        NOT NULL,
    loaded_at            timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (registration_number, publication_date)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'rsh_registered_providers_regnum_idx') THEN
        CREATE INDEX rsh_registered_providers_regnum_idx
            ON rsh_registered_providers (registration_number, snapshot_date);
    END IF;
END $$;
