# W1 Node 5 — revised SQL (S22)

The full `LA Signals` query as stored in the n8n workflow. Held as markdown because `*.sql` is gitignored repo-wide to keep database dumps out; the canonical build artefact is `build_reports/s22_w1_node5_revised.sql` on the pipeline host.

Divergence between this query and `staging_la_signals` is enforced in three places: the `Signal Column Pre-flight` node inside W1, `scripts/w1_contract_check.py` on the scripted path, and a backstop in `scripts/export_map_data.py`.

```sql
-- ===========================================================================
-- W1 Node 5 "LA Signals" — revised for S22 (MHCLG Council Taxbase empty homes)
--
-- Additive only. Every column, join and ON CONFLICT SET entry that was
-- already in the node is retained. The node is a Postgres executeQuery node
-- with queryReplacement = {{ $('Create Run').first().json.run_id }}, so $1 is
-- the run_id. The query is parameterised; no value is concatenated into it.
--
-- Three groups were in the database but not in the stored node, because they
-- were applied to runs 10 and 11 by direct SQL rather than through n8n. They
-- are folded in here so the node and the table agree:
--   S9a/S9b  drd_bed_days_lost, drd_pct_delayed_1plus_days, crfd_days
--   S19      pip_total_claimants, pip_enhanced_daily_living, pip_rate_per_1000
-- New in this revision:
--   S22      ctb_total_dwellings, ctb_empty_6m_plus, ctb_empty_homes_premium,
--            ctb_second_homes, ctb_lte_rate_pct
--
-- The five S22 columns are added to staging_la_signals by the additive
-- migration in scripts/s22_w1_wire.py (DO $$ ... IF NOT EXISTS). The table is
-- never dropped or recreated.
-- ===========================================================================

INSERT INTO staging_la_signals (
    run_id, lad24cd, la_name, population,
    ta_households_current, ta_households_prev_year, ta_yoy_pct, ta_trend_label,
    rough_sleeping_current, rough_sleeping_prev_year,
    care_leavers_semi_indep,
    marac_cases, marac_rate_per_10k,
    hb_sa_caseload, housing_register,
    ro4_bb_spend_000, ro4_nightly_spend_000, ro4_total_homelessness_000,
    efs_flag, s114_flag,
    imd_rank_of_average_rank,
    lha_brma_name, lha_sar_weekly, lha_1bed_weekly,
    lha_2bed_weekly, lha_3bed_weekly, lha_4bed_weekly,
    supported_living_locations,
    drd_bed_days_lost, drd_pct_delayed_1plus_days, crfd_days,
    pip_total_claimants, pip_enhanced_daily_living, pip_rate_per_1000,
    ctb_total_dwellings, ctb_empty_6m_plus, ctb_empty_homes_premium,
    ctb_second_homes, ctb_lte_rate_pct,
    data_quality
)
SELECT
    $1 AS run_id,
    b.lad24cd,
    b.lad24nm AS la_name,
    p.population,

    -- TA signals
    ta_cur.households_in_ta AS ta_households_current,
    ta_prev.households_in_ta AS ta_households_prev_year,
    ROUND(
        (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
        / NULLIF(ta_prev.households_in_ta, 0) * 100
    , 2) AS ta_yoy_pct,
    CASE
        WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap'
        WHEN ta_prev.households_in_ta IS NULL OR ta_prev.households_in_ta = 0 THEN 'no_prior_year'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 10  THEN 'rising_strongly'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > 3   THEN 'rising'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -3  THEN 'flat'
        WHEN (ta_cur.households_in_ta - ta_prev.households_in_ta)::NUMERIC
             / NULLIF(ta_prev.households_in_ta, 0) * 100 > -10 THEN 'falling'
        ELSE 'falling_strongly'
    END AS ta_trend_label,

    -- Rough sleeping
    rs.rough_sleeping AS rough_sleeping_current,
    rs.rough_sleeping_prev_year,

    -- Care leavers (most recent year, 17-21 cohort)
    cl.semi_independent AS care_leavers_semi_indep,

    -- MARAC (via PFA mapping, most recent year)
    mc.cases_discussed AS marac_cases,
    mc.cases_per_10k_adult_females AS marac_rate_per_10k,

    -- HB SA caseload (most recent month)
    sa.hb_sa_claimants AS hb_sa_caseload,

    -- Housing register (most recent year)
    hr.households_on_register AS housing_register,

    -- RO4 spend
    ro4.bb_gross_exp_000 AS ro4_bb_spend_000,
    ro4.nightly_paid_ta_gross_exp_000 AS ro4_nightly_spend_000,
    ro4.total_homelessness_gross_exp_000 AS ro4_total_homelessness_000,

    -- Financial stress flags
    CASE WHEN efs.lad24cd IS NOT NULL THEN TRUE ELSE FALSE END AS efs_flag,
    CASE WHEN s114.lad24cd IS NOT NULL THEN TRUE ELSE FALSE END AS s114_flag,

    -- IMD
    imd.imd_rank_of_average_rank,

    -- LHA rates (S14, via BRMA mapping)
    lbm.brma_name AS lha_brma_name,
    lha.sar_weekly AS lha_sar_weekly,
    lha.one_bed_weekly AS lha_1bed_weekly,
    lha.two_bed_weekly AS lha_2bed_weekly,
    lha.three_bed_weekly AS lha_3bed_weekly,
    lha.four_bed_weekly AS lha_4bed_weekly,

    -- S11 CQC supported living locations (active, non-dormant)
    COALESCE(s11.sl_count, 0) AS supported_living_locations,

    -- S9a DRD / S9b MHSDS CRFD discharge delays
    drd.total_bed_days_lost AS drd_bed_days_lost,
    drd.pct_delayed_1plus_days AS drd_pct_delayed_1plus_days,
    crfd.measure_value AS crfd_days,

    -- S19 PIP claimants. Counts from the table; the rate from
    -- v_la_pip_rates so pip_rate_per_1000 has exactly one definition, the
    -- same treatment ctb_lte_rate_pct gets below. The view also exposes
    -- population_reference_year, because the numerator refreshes monthly and
    -- the denominator annually.
    pip.pip_total_claimants,
    pip.pip_enhanced_daily_living,
    pipr.pip_rate_per_1000,

    -- S22 MHCLG Council Taxbase empty homes (supply-side indicator).
    -- Counts come from the table; the rate comes from
    -- v_la_empty_homes_rates so lte_rate_pct has exactly one definition and
    -- is never recomputed anywhere else.
    ctb.total_dwellings           AS ctb_total_dwellings,
    ctb.empty_6_months_plus       AS ctb_empty_6m_plus,
    ctb.empty_homes_premium_count AS ctb_empty_homes_premium,
    ctb.second_homes              AS ctb_second_homes,
    ctbr.lte_rate_pct             AS ctb_lte_rate_pct,

    -- Data quality flags
    jsonb_build_object(
        'ta_current', CASE WHEN ta_cur.households_in_ta = 0 THEN 'submission_gap' ELSE 'ok' END,
        'ta_trend',   CASE WHEN ta_prev.households_in_ta IS NULL THEN 'no_prior_year' ELSE 'ok' END,
        'rough_sleeping', CASE WHEN rs.rough_sleeping IS NULL THEN 'missing' ELSE 'ok' END,
        'marac',      CASE WHEN mc.cases_discussed IS NULL THEN 'missing' ELSE 'ok' END
    ) AS data_quality

FROM la_boundaries b

-- TA current quarter
LEFT JOIN la_statutory_homelessness ta_cur
    ON ta_cur.lad24cd = b.lad24cd
    AND ta_cur.period = '2025Q2'

-- TA prior year same quarter
LEFT JOIN la_statutory_homelessness ta_prev
    ON ta_prev.lad24cd = b.lad24cd
    AND ta_prev.period = '2024Q2'

-- Population
LEFT JOIN la_population p ON p.lad24cd = b.lad24cd

-- Rough sleeping
LEFT JOIN la_rough_sleeping rs
    ON rs.lad24cd = b.lad24cd
    AND rs.snapshot_year = 2025

-- Care leavers (most recent year available = 2024)
LEFT JOIN care_leaver_accommodation cl
    ON cl.lad24cd = b.lad24cd
    AND cl.reporting_year = 2024
    AND cl.age_group = '17-21'

-- MARAC (via PFA mapping, most recent year = 2024-25)
LEFT JOIN la_pfa_mapping pfa ON pfa.lad24cd = b.lad24cd
LEFT JOIN marac_cases mc
    ON mc.pfa_name_safelives = pfa.pfa_name_safelives
    AND mc.financial_year = '2024-25'

-- HB SA caseload (most recent month)
LEFT JOIN la_hb_sa_caseload sa
    ON sa.lad24cd = b.lad24cd
    AND sa.month = (SELECT MAX(month) FROM la_hb_sa_caseload)

-- Housing register (most recent year)
LEFT JOIN la_housing_register hr
    ON hr.lad24cd = b.lad24cd
    AND hr.reporting_year = (SELECT MAX(reporting_year) FROM la_housing_register)

-- RO4 spend
LEFT JOIN ro4_housing_expenditure ro4
    ON ro4.lad24cd = b.lad24cd
    AND ro4.financial_year = '2024-25'

-- EFS (any year flagged)
LEFT JOIN (
    SELECT DISTINCT lad24cd FROM la_efs_support
    WHERE financial_year IN ('2024-25', '2025-26')
) efs ON efs.lad24cd = b.lad24cd

-- S114 (any notice on record)
LEFT JOIN (
    SELECT DISTINCT lad24cd FROM la_s114_notices
) s114 ON s114.lad24cd = b.lad24cd

-- IMD
LEFT JOIN la_imd_2025 imd ON imd.lad24cd = b.lad24cd

-- LHA rates (S14)
LEFT JOIN la_brma_mapping lbm ON lbm.lad24cd = b.lad24cd
LEFT JOIN brma_lha_rates lha
    ON lha.brma_name = lbm.brma_name
    AND lha.financial_year = '2026-27'

-- S11 supported living counts
LEFT JOIN (
    SELECT lad24cd, COUNT(*) AS sl_count
    FROM cqc_locations
    WHERE supported_living AND is_active AND NOT dormant
    GROUP BY lad24cd
) s11 ON s11.lad24cd = b.lad24cd

-- S9a DRD (latest reporting period)
LEFT JOIN vw_drd_discharge_delays_lad drd
    ON drd.lad24cd = b.lad24cd
    AND drd.reporting_period = (SELECT MAX(reporting_period)
                                  FROM nhs_drd_discharge_delays)

-- S9b MHSDS CRFD (latest reporting period, MHS26)
LEFT JOIN vw_mh_crfd_lad crfd
    ON crfd.lad24cd = b.lad24cd
    AND crfd.reporting_period = (SELECT MAX(reporting_period)
                                   FROM nhs_mh_crfd)
    AND crfd.measure_id = 'MHS26'

-- S19 PIP claimants (latest month)
LEFT JOIN la_pip_claimants pip
    ON pip.lad24cd = b.lad24cd
    AND pip.month = (SELECT MAX(month) FROM la_pip_claimants)
LEFT JOIN v_la_pip_rates pipr
    ON pipr.lad24cd = b.lad24cd
    AND pipr.month = (SELECT MAX(month) FROM la_pip_claimants)

-- S22 Council Taxbase empty homes (latest taxbase year)
LEFT JOIN la_council_taxbase_empties ctb
    ON ctb.lad24cd = b.lad24cd
    AND ctb.taxbase_year = (SELECT MAX(taxbase_year)
                              FROM la_council_taxbase_empties)
LEFT JOIN v_la_empty_homes_rates ctbr
    ON ctbr.lad24cd = b.lad24cd

ON CONFLICT (run_id, lad24cd) DO UPDATE SET
    ta_households_current      = EXCLUDED.ta_households_current,
    ta_households_prev_year    = EXCLUDED.ta_households_prev_year,
    ta_yoy_pct                 = EXCLUDED.ta_yoy_pct,
    ta_trend_label             = EXCLUDED.ta_trend_label,
    rough_sleeping_current     = EXCLUDED.rough_sleeping_current,
    care_leavers_semi_indep    = EXCLUDED.care_leavers_semi_indep,
    marac_cases                = EXCLUDED.marac_cases,
    hb_sa_caseload             = EXCLUDED.hb_sa_caseload,
    efs_flag                   = EXCLUDED.efs_flag,
    s114_flag                  = EXCLUDED.s114_flag,
    imd_rank_of_average_rank   = EXCLUDED.imd_rank_of_average_rank,
    lha_brma_name              = EXCLUDED.lha_brma_name,
    lha_sar_weekly             = EXCLUDED.lha_sar_weekly,
    lha_1bed_weekly            = EXCLUDED.lha_1bed_weekly,
    lha_2bed_weekly            = EXCLUDED.lha_2bed_weekly,
    lha_3bed_weekly            = EXCLUDED.lha_3bed_weekly,
    lha_4bed_weekly            = EXCLUDED.lha_4bed_weekly,
    supported_living_locations = EXCLUDED.supported_living_locations,
    drd_bed_days_lost          = EXCLUDED.drd_bed_days_lost,
    drd_pct_delayed_1plus_days = EXCLUDED.drd_pct_delayed_1plus_days,
    crfd_days                  = EXCLUDED.crfd_days,
    pip_total_claimants        = EXCLUDED.pip_total_claimants,
    pip_enhanced_daily_living  = EXCLUDED.pip_enhanced_daily_living,
    pip_rate_per_1000          = EXCLUDED.pip_rate_per_1000,
    ctb_total_dwellings        = EXCLUDED.ctb_total_dwellings,
    ctb_empty_6m_plus          = EXCLUDED.ctb_empty_6m_plus,
    ctb_empty_homes_premium    = EXCLUDED.ctb_empty_homes_premium,
    ctb_second_homes           = EXCLUDED.ctb_second_homes,
    ctb_lte_rate_pct           = EXCLUDED.ctb_lte_rate_pct,
    data_quality               = EXCLUDED.data_quality;
```
