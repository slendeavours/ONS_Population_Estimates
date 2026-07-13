# W1 Node 5 Patch: S9 Discharge Delay Columns

## Overview

Adds three new columns to `staging_la_signals` from S9a (DRD) and S9b (MHSDS CRFD) sources.

## New Columns

| Column | Source | Type | Description |
|---|---|---|---|
| `drd_bed_days_lost` | `vw_drd_discharge_delays_lad` | INTEGER | Total bed days lost to delayed discharge. Population-weighted for county→district rows. |
| `drd_pct_delayed_1plus_days` | `vw_drd_discharge_delays_lad` | NUMERIC | Percentage of discharges delayed 1+ days. **UTLA-level pass-through: districts under a county share the same value.** |
| `crfd_days` | `vw_mh_crfd_lad` | INTEGER | MHS26 CRFD delayed discharge days (combined MH+LD/autism). Direct LA-level data via code-resolving view. |

## New JOINs

Add after existing LHA/CQC joins:

```sql
LEFT JOIN vw_drd_discharge_delays_lad drd
    ON drd.lad24cd = b.lad24cd
    AND drd.reporting_period = (SELECT MAX(reporting_period) FROM nhs_drd_discharge_delays)
LEFT JOIN vw_mh_crfd_lad crfd
    ON crfd.lad24cd = b.lad24cd
    AND crfd.reporting_period = (SELECT MAX(reporting_period) FROM nhs_mh_crfd)
    AND crfd.measure_id = 'MHS26'
```

## New SELECT Columns

```sql
drd.total_bed_days_lost AS drd_bed_days_lost,
drd.pct_delayed_1plus_days AS drd_pct_delayed_1plus_days,
crfd.measure_value AS crfd_days,
```

## New ON CONFLICT SET

```sql
drd_bed_days_lost = EXCLUDED.drd_bed_days_lost,
drd_pct_delayed_1plus_days = EXCLUDED.drd_pct_delayed_1plus_days,
crfd_days = EXCLUDED.crfd_days,
```

## Code Resolution

`vw_mh_crfd_lad` resolves Barnsley (E08000038→E08000016) and Sheffield (E08000039→E08000019) via `la_code_lookup` so that MHSDS codes from June 2025+ join correctly to `la_boundaries`.

## Node 9 Export Patch

Add to the GeoJSON properties object:

```sql
'drd_bed_days_lost', sig.drd_bed_days_lost,
'drd_pct_delayed_1plus_days', sig.drd_pct_delayed_1plus_days,
'crfd_days', sig.crfd_days,
```

## Run 10 Coverage

| Column | Populated | NULL |
|---|---|---|
| drd_bed_days_lost | 296/296 | 0 |
| drd_pct_delayed_1plus_days | 296/296 | 0 |
| crfd_days | 205/296 | 91 (suppressed at source) |
