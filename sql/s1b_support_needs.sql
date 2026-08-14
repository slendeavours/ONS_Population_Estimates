-- S1b - MHCLG statutory homelessness Table A3 support needs
--
-- Generated from scripts/s1b_support_needs_build.py on 2026-08-14. That script is the
-- authority: it executes this DDL at load time, so this file is a
-- readable copy for review and not a second definition to maintain.
-- Additive only - CREATE TABLE IF NOT EXISTS, every ALTER guarded.

CREATE TABLE IF NOT EXISTS la_homelessness_support_needs (
    lad24cd            varchar(9)  NOT NULL,
    period             varchar(6)  NOT NULL,
    category_code      text        NOT NULL,
    value              integer,
    value_flag         text,
    category_group     text        NOT NULL,
    category_label     text        NOT NULL,
    reference_quarter  varchar(7)  NOT NULL,
    source_url         text        NOT NULL,
    source_edition     text        NOT NULL,
    edition_variant    text        NOT NULL,
    release_page_url   text        NOT NULL,
    layout_version     text        NOT NULL,
    publisher_la_code  varchar(9)  NOT NULL,
    loaded_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, period, category_code)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_value_flag_chk') THEN
        ALTER TABLE la_homelessness_support_needs ADD CONSTRAINT la_homelessness_support_needs_value_flag_chk
            CHECK (value_flag IS NULL
                   OR value_flag IN ('missing','suppressed','not_applicable'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_value_xor_flag_chk') THEN
        ALTER TABLE la_homelessness_support_needs ADD CONSTRAINT la_homelessness_support_needs_value_xor_flag_chk
            CHECK (num_nonnulls(value, value_flag) = 1);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_category_group_chk') THEN
        ALTER TABLE la_homelessness_support_needs ADD CONSTRAINT la_homelessness_support_needs_category_group_chk
            CHECK (category_group IN
                   ('support_need','needs_breakdown','needs_total','duty_total'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'la_homelessness_support_needs_edition_variant_chk') THEN
        ALTER TABLE la_homelessness_support_needs ADD CONSTRAINT la_homelessness_support_needs_edition_variant_chk
            CHECK (edition_variant IN
                   ('original','revised','corrected','fixed'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'la_homelessness_support_needs_period_category_idx') THEN
        CREATE INDEX la_homelessness_support_needs_period_category_idx
            ON la_homelessness_support_needs (period, category_code);
    END IF;
END $$;
