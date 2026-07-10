# W1 Node 5: la_signals (UPDATED with LHA Joins)

## Overview
This is the updated version of W1 Node 5 SQL query that adds LHA (Local Housing Allowance) rate data to the staging_la_signals table. The new LHA columns link each LA to its BRMA and include the weekly rates for all bedroom categories.

## Required Changes
Add two new LEFT JOIN clauses and six new SELECT columns to the existing W1 Node 5 query.

## New Joins (add after existing la_imd_2025 join)

```sql
LEFT JOIN la_brma_mapping lbm ON lbm.lad24cd = b.lad24cd
LEFT JOIN brma_lha_rates lha ON lha.brma_name = lbm.brma_name AND lha.financial_year = '2026-27'
```

These joins:
1. Link each LA to its BRMA (via la_brma_mapping)
2. Link each BRMA to its LHA rates for FY 2026-27
3. Use LEFT JOIN so LAs without a mapping don't cause the row to drop (NULL values instead)

## New SELECT Columns (add to the SELECT list)

```sql
lbm.brma_name AS lha_brma_name,
lha.sar_weekly AS lha_sar_weekly,
lha.one_bed_weekly AS lha_1bed_weekly,
lha.two_bed_weekly AS lha_2bed_weekly,
lha.three_bed_weekly AS lha_3bed_weekly,
lha.four_bed_weekly AS lha_4bed_weekly,
```

These columns:
- lha_brma_name: The BRMA this LA maps to
- lha_sar_weekly: Single Adult Rate (GBP/week)
- lha_1bed_weekly through lha_4bed_weekly: Rates for 1-4 bedroom properties (GBP/week)

All rates are weekly figures (converted from DWP monthly publication).

## New ON CONFLICT SET Entries (add to the UPDATE clause)

```sql
lha_brma_name = EXCLUDED.lha_brma_name,
lha_sar_weekly = EXCLUDED.lha_sar_weekly,
lha_1bed_weekly = EXCLUDED.lha_1bed_weekly,
lha_2bed_weekly = EXCLUDED.lha_2bed_weekly,
lha_3bed_weekly = EXCLUDED.lha_3bed_weekly,
lha_4bed_weekly = EXCLUDED.lha_4bed_weekly,
```

These ensure the LHA columns are updated if the row already exists.

## Complete Template Query

```sql
INSERT INTO staging_la_signals (
    run_id,
    lad24cd,
    -- existing columns --
    la_name,
    ta_caseload,
    rough_sleeping_count,
    care_leavers_count,
    marac_cases,
    hb_sa_caseload,
    housing_register_size,
    ro4_spend,
    efs_s114_flags,
    imd_rank,
    -- NEW LHA columns --
    lha_brma_name,
    lha_sar_weekly,
    lha_1bed_weekly,
    lha_2bed_weekly,
    lha_3bed_weekly,
    lha_4bed_weekly
)
SELECT
    '{{ run_id }}' AS run_id,
    b.lad24cd,
    -- existing columns --
    b.lad24nm,
    -- ... (keep all existing column selections) ...
    imd2025.rank,
    -- NEW LHA columns --
    lbm.brma_name AS lha_brma_name,
    lha.sar_weekly AS lha_sar_weekly,
    lha.one_bed_weekly AS lha_1bed_weekly,
    lha.two_bed_weekly AS lha_2bed_weekly,
    lha.three_bed_weekly AS lha_3bed_weekly,
    lha.four_bed_weekly AS lha_4bed_weekly
FROM
    la_boundaries b
    LEFT JOIN la_imd_2025 imd2025 ON imd2025.lad24cd = b.lad24cd
    -- ... (keep all existing joins) ...
    -- NEW JOINS (add after existing joins) --
    LEFT JOIN la_brma_mapping lbm ON lbm.lad24cd = b.lad24cd
    LEFT JOIN brma_lha_rates lha ON lha.brma_name = lbm.brma_name AND lha.financial_year = '2026-27'
ON CONFLICT (run_id, lad24cd) DO UPDATE SET
    -- existing SET clauses --
    imd_rank = EXCLUDED.imd_rank,
    -- ... (keep all existing update clauses) ...
    -- NEW SET clauses --
    lha_brma_name = EXCLUDED.lha_brma_name,
    lha_sar_weekly = EXCLUDED.lha_sar_weekly,
    lha_1bed_weekly = EXCLUDED.lha_1bed_weekly,
    lha_2bed_weekly = EXCLUDED.lha_2bed_weekly,
    lha_3bed_weekly = EXCLUDED.lha_3bed_weekly,
    lha_4bed_weekly = EXCLUDED.lha_4bed_weekly;
```

## Notes
- Replace `{{ run_id }}` with your actual run ID placeholder
- Keep all existing SELECT columns, JOINs, and WHERE clauses
- The new LHA columns are ADDITIVE - do not remove existing columns
- All LHA rates are in GBP/week (converted from DWP monthly publication)
- NULL values will appear for any LAs that don't have a BRMA mapping (should be none with S14 complete)
- Update the existing column list in the INSERT and ON CONFLICT clauses with these new entries

## Testing
After updating the query in n8n:
1. Run the workflow
2. Verify staging_la_signals now has lha_* columns populated
3. Spot-check: SELECT * FROM staging_la_signals WHERE lad24cd='E08000025' (Birmingham should have SAR ~78.83)
