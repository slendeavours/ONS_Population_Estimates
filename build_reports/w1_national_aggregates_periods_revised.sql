INSERT INTO staging_national (
    run_id,
    ta_households_current,
    ta_households_prev_year,
    ta_yoy_pct,
    rough_sleeping_current,
    rough_sleeping_prev_year,
    bb_spend_total_000,
    nightly_paid_spend_total_000,
    hb_sa_caseload_total,
    housing_register_total
)
SELECT
    $1 AS run_id,
    -- TA current (2025Q2 = most recent loaded)
    SUM(CASE WHEN period = (SELECT MAX(period) FROM la_statutory_homelessness) THEN households_in_ta ELSE 0 END),
    -- TA prior year (2024Q2)
    SUM(CASE WHEN period = (SELECT (LEFT(MAX(period), 4)::int - 1)::text || RIGHT(MAX(period), 2) FROM la_statutory_homelessness) THEN households_in_ta ELSE 0 END),
    -- YoY %
    ROUND(
        (SUM(CASE WHEN period = (SELECT MAX(period) FROM la_statutory_homelessness) THEN households_in_ta ELSE 0 END)::NUMERIC
        - SUM(CASE WHEN period = (SELECT (LEFT(MAX(period), 4)::int - 1)::text || RIGHT(MAX(period), 2) FROM la_statutory_homelessness) THEN households_in_ta ELSE 0 END)::NUMERIC)
        / NULLIF(SUM(CASE WHEN period = (SELECT (LEFT(MAX(period), 4)::int - 1)::text || RIGHT(MAX(period), 2) FROM la_statutory_homelessness) THEN households_in_ta ELSE 0 END), 0) * 100
    , 2),
    -- Rough sleeping current
    (SELECT SUM(rough_sleeping) FROM la_rough_sleeping WHERE snapshot_year = (SELECT MAX(snapshot_year)
                             FROM la_rough_sleeping)),
    -- Rough sleeping prior year
    (SELECT SUM(rough_sleeping_prev_year) FROM la_rough_sleeping WHERE snapshot_year = 2025),
    -- B&B spend
    (SELECT SUM(bb_gross_exp_000) FROM ro4_housing_expenditure WHERE financial_year = (SELECT MAX(financial_year)
                              FROM ro4_housing_expenditure)),
    -- Nightly paid spend
    (SELECT SUM(nightly_paid_ta_gross_exp_000) FROM ro4_housing_expenditure
     WHERE financial_year = (SELECT MAX(financial_year)
                             FROM ro4_housing_expenditure)),
    -- HB SA caseload (most recent month)
    -- Sourced from S8b since 2026-08-14; S8 is superseded.
    (SELECT SUM(claimants) FROM la_hb_accom_type_caseload
     WHERE accom_type = 'SA'
       AND month = (SELECT MAX(month) FROM la_hb_accom_type_caseload
                    WHERE accom_type = 'SA')),
    -- Housing register
    (SELECT SUM(households_on_register) FROM la_housing_register
     WHERE reporting_year = (SELECT MAX(reporting_year) FROM la_housing_register))
FROM la_statutory_homelessness
WHERE period IN (
    (SELECT MAX(period) FROM la_statutory_homelessness),
    (SELECT (LEFT(MAX(period), 4)::int - 1)::text || RIGHT(MAX(period), 2)
     FROM la_statutory_homelessness))
AND households_in_ta > 0;