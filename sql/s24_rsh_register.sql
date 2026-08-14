-- S24 - RSH register of providers and regulatory judgements
--
-- Generated from scripts/s24_rsh_register_build.py on 2026-08-14. That script is the
-- authority: it executes this DDL at load time, so this file is a
-- readable copy for review and not a second definition to maintain.
-- Additive only - CREATE TABLE IF NOT EXISTS, every ALTER guarded.

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
