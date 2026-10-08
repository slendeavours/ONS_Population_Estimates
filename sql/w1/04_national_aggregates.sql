WITH p AS (
    SELECT (SELECT MAX(period) FROM la_statutory_homelessness) AS cur_p,
           (SELECT (LEFT(MAX(period), 4)::int - 1)::text || RIGHT(MAX(period), 2)
            FROM la_statutory_homelessness) AS prev_p
),
c AS (
    SELECT s.lad24cd, s.households_in_ta
    FROM la_statutory_homelessness s, p
    WHERE s.period = p.cur_p AND s.households_in_ta > 0
),
pr AS (
    SELECT s.lad24cd, s.households_in_ta
    FROM la_statutory_homelessness s, p
    WHERE s.period = p.prev_p AND s.households_in_ta > 0
),
m AS (
    -- like-for-like: authorities reporting TA in BOTH periods
    SELECT c.lad24cd, c.households_in_ta AS cur_h, pr.households_in_ta AS prev_h
    FROM c JOIN pr USING (lad24cd)
)
INSERT INTO staging_national (
    run_id,
    ta_households_current,
    ta_households_prev_year,
    ta_yoy_pct,
    ta_matched_authorities,
    ta_households_current_matched,
    ta_households_prev_year_matched,
    rough_sleeping_current,
    rough_sleeping_prev_year,
    bb_spend_total_000,
    nightly_paid_spend_total_000,
    hb_sa_caseload_total,
    housing_register_total
)
SELECT
    $1 AS run_id,
    -- England total of authorities reporting TA in the latest period
    (SELECT SUM(households_in_ta) FROM c),
    -- England total of authorities reporting TA a year earlier. Not comparable
    -- with the line above: the two sets of authorities differ.
    (SELECT SUM(households_in_ta) FROM pr),
    -- YoY over the matched set only
    ROUND(((SELECT SUM(cur_h) FROM m)::NUMERIC - (SELECT SUM(prev_h) FROM m)::NUMERIC)
          / NULLIF((SELECT SUM(prev_h) FROM m), 0) * 100, 2),
    (SELECT COUNT(*) FROM m),
    (SELECT SUM(cur_h) FROM m),
    (SELECT SUM(prev_h) FROM m),
    -- Rough sleeping current
    (SELECT SUM(rough_sleeping) FROM la_rough_sleeping WHERE snapshot_year = (SELECT MAX(snapshot_year)
                             FROM la_rough_sleeping)),
    -- Rough sleeping prior year
    (SELECT SUM(rough_sleeping_prev_year) FROM la_rough_sleeping WHERE snapshot_year = (SELECT MAX(snapshot_year)
                             FROM la_rough_sleeping)),
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
     WHERE reporting_year = (SELECT MAX(reporting_year) FROM la_housing_register));
