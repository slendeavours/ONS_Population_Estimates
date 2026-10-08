INSERT INTO staging_tenant_type_rankings
    (run_id, tenant_type, rank_position, lad24cd, la_name, primary_signal, signal_label, data_confidence)

SELECT $1::integer, 'single_homeless', rank_position, lad24cd, la_name,
       ta_households_current, 'households_in_ta_2025Q2', 'High'
FROM (
    SELECT lad24cd, la_name, ta_households_current,
           ROW_NUMBER() OVER (ORDER BY ta_households_current DESC NULLS LAST) AS rank_position
    FROM staging_la_signals
    WHERE run_id = $1::integer
    AND ta_households_current > 0
    AND (data_quality->>'ta_current') = 'ok'
) t WHERE rank_position <= 296

UNION ALL

SELECT $1::integer, 'rough_sleepers', rank_position, lad24cd, la_name,
       rough_sleeping_current, 'rough_sleeping_2025', 'High'
FROM (
    SELECT lad24cd, la_name, rough_sleeping_current,
           ROW_NUMBER() OVER (ORDER BY rough_sleeping_current DESC NULLS LAST) AS rank_position
    FROM staging_la_signals
    WHERE run_id = $1::integer
    AND rough_sleeping_current IS NOT NULL
) t WHERE rank_position <= 296

UNION ALL

SELECT $1::integer, 'care_leavers', rank_position, lad24cd, la_name,
       care_leavers_semi_indep, 'semi_independent_2024', 'High'
FROM (
    SELECT lad24cd, la_name, care_leavers_semi_indep,
           ROW_NUMBER() OVER (ORDER BY care_leavers_semi_indep DESC NULLS LAST) AS rank_position
    FROM staging_la_signals
    WHERE run_id = $1::integer
    AND care_leavers_semi_indep IS NOT NULL
) t WHERE rank_position <= 296

UNION ALL

SELECT $1::integer, 'domestic_abuse', rank_position, lad24cd, la_name,
       marac_rate_per_10k, 'marac_cases_per_10k_adult_females_2024_25', 'Medium'
FROM (
    SELECT lad24cd, la_name, marac_rate_per_10k,
           ROW_NUMBER() OVER (ORDER BY marac_rate_per_10k DESC NULLS LAST) AS rank_position
    FROM staging_la_signals
    WHERE run_id = $1::integer
    AND marac_rate_per_10k IS NOT NULL
) t WHERE rank_position <= 296

UNION ALL

SELECT $1::integer, 'older_complex', rank_position, lad24cd, la_name,
       hb_sa_caseload, 'hb_sa_claimants_latest_month', 'Medium'
FROM (
    SELECT lad24cd, la_name, hb_sa_caseload,
           ROW_NUMBER() OVER (ORDER BY hb_sa_caseload DESC NULLS LAST) AS rank_position
    FROM staging_la_signals
    WHERE run_id = $1::integer
    AND hb_sa_caseload IS NOT NULL
) t WHERE rank_position <= 296

ON CONFLICT (run_id, tenant_type, rank_position) DO UPDATE SET
    lad24cd        = EXCLUDED.lad24cd,
    la_name        = EXCLUDED.la_name,
    primary_signal = EXCLUDED.primary_signal;