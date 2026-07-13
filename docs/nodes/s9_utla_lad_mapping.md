# S9 UTLA-to-LAD Mapping

## Purpose

Maps upper-tier local authority (UTLA) codes used in NHS DRD publications to LAD24CD codes used throughout the pipeline. Required because DRD data is published at UTLA geography (E06/E08/E09 unitaries + E10 counties), not at LAD24CD directly.

## Table

```sql
utla_lad_mapping
```

**Primary key:** `(utla_code, lad24cd)`

## Method

| UTLA prefix | Mapping method | Weight | Count |
|---|---|---|---|
| E06 (unitary) | `direct` | 1.0 | 58 |
| E08 (metropolitan) | `direct` | 1.0 | 36 |
| E09 (London borough) | `direct` | 1.0 | 33 |
| E10 (county) | `population_weighted` | pop share | 164 |

**Total:** 296 rows (one per LAD24CD).

## Population Weighting

For E10 counties, each constituent E07 district receives a weight equal to its share of the county's total population. Weights are derived from `la_population` (reference year 2024) and the ONS `LAD24_CTY24_EN_LU` lookup (233 records from ArcGIS FeatureServer).

## Verification Results

| Check | Result |
|---|---|
| Weights sum to 1.0 per UTLA (±0.0001) | PASS |
| All 296 LAD24CDs appear exactly once | PASS |
| Zero DRD UTLA orphans | PASS |
| E06/E08/E09 rows: method=direct, weight=1.0 | PASS |
| E10 rows: method=population_weighted, weight<1.0 | PASS |

## LAD-Level View

```sql
vw_drd_discharge_delays_lad
```

Joins `nhs_drd_discharge_delays` to `utla_lad_mapping` and applies population-weighted apportionment to count columns. Percentage and average columns pass through at UTLA level — **all districts under a county inherit the county's percentage/average values** (known resolution limitation).

## Source

- ONS Open Geography Portal: `LAD24_CTY24_EN_LU` FeatureServer
- Pipeline table: `la_population` (2024 mid-year estimates)
