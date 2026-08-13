# Node 7 — Create Rates View

## Type

Postgres — Execute Query. `scripts/s22_ctb_empties_build.py`, constant `VIEW_SQL`.

## Purpose

Expose the four derived empty homes rates over the latest taxbase year. Rates live here and are never stored in a table.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

```sql
DROP VIEW IF EXISTS v_la_empty_homes_rates;
CREATE VIEW v_la_empty_homes_rates AS
SELECT
    e.lad24cd,
    e.la_name,
    e.taxbase_year,
    e.total_dwellings,
    e.empty_total,
    e.empty_6_months_plus,
    e.empty_homes_premium_count,
    e.second_homes,
    ROUND(e.empty_6_months_plus::NUMERIC
          / NULLIF(e.total_dwellings, 0) * 100, 2)      AS lte_rate_pct,
    ROUND(e.empty_6_months_plus::NUMERIC
          / NULLIF(e.empty_total, 0) * 100, 2)          AS lte_share_of_empties_pct,
    ROUND(e.empty_homes_premium_count::NUMERIC
          / NULLIF(e.empty_6_months_plus, 0) * 100, 2)  AS premium_coverage_pct,
    ROUND(e.second_homes::NUMERIC
          / NULLIF(e.total_dwellings, 0) * 100, 2)      AS second_homes_rate_pct
FROM la_council_taxbase_empties e
WHERE e.taxbase_year = (SELECT MAX(taxbase_year)
                          FROM la_council_taxbase_empties);

COMMENT ON VIEW v_la_empty_homes_rates IS
 'Derived empty homes rates over the latest taxbase year in '
 'la_council_taxbase_empties. Rates are computed here and never stored.';
COMMENT ON COLUMN v_la_empty_homes_rates.lte_rate_pct IS
 'Long-term empty (6 months or more) as a percentage of all dwellings on the '
 'valuation list.';
COMMENT ON COLUMN v_la_empty_homes_rates.lte_share_of_empties_pct IS
 'Long-term empty as a percentage of all dwellings classed as empty.';
COMMENT ON COLUMN v_la_empty_homes_rates.premium_coverage_pct IS
 'Directional only. This can never reach 100: long-term empty starts at six '
 'months while the Empty Homes Premium starts at twelve, so the numerator is '
 'drawn from a strictly narrower population than the denominator. It '
 'indicates how far an authority applies the premium across its long-term '
 'empty stock, and is not a compliance rate.';
COMMENT ON COLUMN v_la_empty_homes_rates.second_homes_rate_pct IS
 'Second homes as a percentage of all dwellings on the valuation list.';
```

## Query Parameters

None.

## Behaviour

`DROP VIEW IF EXISTS` then `CREATE VIEW`, so the definition is replaced wholesale on each run and cannot drift from the script.

The year filter is `MAX(taxbase_year)` rather than a literal, so the view follows the data forward when the November 2026 release lands without any edit.

Every denominator is wrapped in `NULLIF`, so an authority with zero dwellings on any measure yields null rather than a division error.

The `premium_coverage_pct` caveat is a column comment, not only prose in a document. Anyone inspecting the view in a client sees it.

## Connection

- Input: Node 6 (Upsert Table 615 and Seed Series Breaks)
- Output: Node 8 (Hard Gates)

## Verified Output

2026-08-13. View created over taxbase year 2025, 296 rows. `lte_rate_pct` ranges from 0.38 (Havant) to 3.34 (Isles of Scilly); no value falls outside 0 to 100. Highest rates: Isles of Scilly 3.34, Torbay 2.44, North East Lincolnshire 2.33, Westmorland and Furness 2.30, Kensington and Chelsea 2.28. Lowest: Havant 0.38, Tandridge 0.42, South Gloucestershire 0.49.
