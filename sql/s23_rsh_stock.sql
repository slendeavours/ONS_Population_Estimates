-- S23 - RSH registered provider stock by local authority
--
-- Generated from scripts/s23_rsh_stock_build.py on 2026-08-14. That script is the
-- authority: it executes this DDL at load time, so this file is a
-- readable copy for review and not a second definition to maintain.
-- Additive only - CREATE TABLE IF NOT EXISTS, every ALTER guarded.

CREATE TABLE IF NOT EXISTS rsh_rp_stock_by_la (
    stock_date                          date        NOT NULL,
    rp_code                             text        NOT NULL,
    lad24cd                             varchar(9)  NOT NULL,
    rp_name                             text        NOT NULL,
    provider_type                       text        NOT NULL,
    rp_size_band                        text,
    survey_status                       text,
    publisher_la_code                   varchar(9)  NOT NULL,
    la_name                             text        NOT NULL,
    total_social_stock                  integer     NOT NULL,
    general_needs_self_contained        integer     NOT NULL,
    general_needs_bedspaces             integer     NOT NULL,
    supported_housing_and_older_people  integer     NOT NULL,
    low_cost_home_ownership             integer     NOT NULL,
    publication_date                    date        NOT NULL,
    edition                             text        NOT NULL,
    source_url                          text        NOT NULL,
    source_file                         text        NOT NULL,
    release_page_url                    text        NOT NULL,
    loaded_at                           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stock_date, rp_code, lad24cd)
);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rsh_rp_stock_by_la_provider_type_chk') THEN
        ALTER TABLE rsh_rp_stock_by_la ADD CONSTRAINT rsh_rp_stock_by_la_provider_type_chk
            CHECK (provider_type IN ('PRP','LARP'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conname = 'rsh_rp_stock_by_la_components_sum_chk') THEN
        ALTER TABLE rsh_rp_stock_by_la ADD CONSTRAINT rsh_rp_stock_by_la_components_sum_chk
            CHECK (total_social_stock = general_needs_self_contained
                                      + general_needs_bedspaces
                                      + supported_housing_and_older_people
                                      + low_cost_home_ownership);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_indexes
                   WHERE indexname = 'rsh_rp_stock_by_la_lad24cd_idx') THEN
        CREATE INDEX rsh_rp_stock_by_la_lad24cd_idx ON rsh_rp_stock_by_la (lad24cd, stock_date);
    END IF;
END $$;
