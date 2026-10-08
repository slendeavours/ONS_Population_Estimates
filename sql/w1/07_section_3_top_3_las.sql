INSERT INTO staging_convergence
    (run_id, lad24cd, la_name, section3_rank, tenant_types_in_top3, convergence_count)

WITH tenant_appearances AS (
    SELECT lad24cd, la_name,
           ARRAY_AGG(tenant_type ORDER BY tenant_type) AS tenant_types_in_top3,
           COUNT(*) AS convergence_count
    FROM staging_tenant_type_rankings
    WHERE run_id = $1::integer
    AND rank_position <= 3
    GROUP BY lad24cd, la_name
)
SELECT
    $1::integer, s.lad24cd, s.la_name,
    ROW_NUMBER() OVER (
        ORDER BY COALESCE(t.convergence_count, 0) DESC,
                 COALESCE(s.ta_households_current, 0) DESC
    )::integer AS section3_rank,
    COALESCE(t.tenant_types_in_top3, ARRAY[]::TEXT[]) AS tenant_types_in_top3,
    COALESCE(t.convergence_count, 0) AS convergence_count
FROM staging_la_signals s
LEFT JOIN tenant_appearances t ON t.lad24cd = s.lad24cd
WHERE s.run_id = $1::integer

ON CONFLICT (run_id, lad24cd) DO UPDATE SET
    section3_rank        = EXCLUDED.section3_rank,
    tenant_types_in_top3 = EXCLUDED.tenant_types_in_top3,
    convergence_count    = EXCLUDED.convergence_count;