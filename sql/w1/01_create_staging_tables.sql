CREATE TABLE IF NOT EXISTS staging_runs (
    run_id          SERIAL PRIMARY KEY,
    run_date        TIMESTAMPTZ DEFAULT NOW(),
    status          TEXT DEFAULT 'in_progress',
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS staging_national (
    run_id                          INTEGER NOT NULL,
    ta_households_current           INTEGER,
    ta_households_prev_year         INTEGER,
    ta_yoy_pct                      NUMERIC(8,2),
    ta_matched_authorities          INTEGER,
    ta_households_current_matched   INTEGER,
    ta_households_prev_year_matched INTEGER,
    rough_sleeping_current          INTEGER,
    rough_sleeping_prev_year        INTEGER,
    bb_spend_total_000              NUMERIC(14,2),
    nightly_paid_spend_total_000    NUMERIC(14,2),
    hb_sa_caseload_total            INTEGER,
    housing_register_total          INTEGER,
    PRIMARY KEY (run_id)
);

CREATE TABLE IF NOT EXISTS staging_la_signals (
    run_id                      INTEGER NOT NULL,
    lad24cd                     VARCHAR(9) NOT NULL,
    la_name                     VARCHAR(100),
    population                  INTEGER,
    ta_households_current       INTEGER,
    ta_households_prev_year     INTEGER,
    ta_yoy_pct                  NUMERIC(8,2),
    ta_trend_label              TEXT,
    rough_sleeping_current      INTEGER,
    rough_sleeping_prev_year    INTEGER,
    care_leavers_semi_indep     INTEGER,
    marac_cases                 NUMERIC(10,2),
    marac_rate_per_10k          NUMERIC(10,6),
    hb_sa_caseload              INTEGER,
    housing_register            INTEGER,
    ro4_bb_spend_000            NUMERIC(12,2),
    ro4_nightly_spend_000       NUMERIC(12,2),
    ro4_total_homelessness_000  NUMERIC(12,2),
    efs_flag                    BOOLEAN DEFAULT FALSE,
    s114_flag                   BOOLEAN DEFAULT FALSE,
    imd_rank_of_average_rank    INTEGER,
    data_quality                JSONB,
    PRIMARY KEY (run_id, lad24cd)
);

CREATE TABLE IF NOT EXISTS staging_tenant_type_rankings (
    run_id          INTEGER NOT NULL,
    tenant_type     TEXT NOT NULL,
    rank_position   INTEGER NOT NULL,
    lad24cd         VARCHAR(9) NOT NULL,
    la_name         VARCHAR(100),
    primary_signal  NUMERIC,
    signal_label    TEXT,
    data_confidence TEXT,
    PRIMARY KEY (run_id, tenant_type, rank_position)
);

CREATE TABLE IF NOT EXISTS staging_convergence (
    run_id                  INTEGER NOT NULL,
    lad24cd                 VARCHAR(9) NOT NULL,
    la_name                 VARCHAR(100),
    section3_rank           INTEGER,
    tenant_types_in_top3    TEXT[],
    convergence_count       INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, lad24cd)
);