# S9a Node 3: Upsert DRD Discharge Delays

- **Type:** SQL (Postgres)
- **Purpose:** Idempotent upsert of parsed UTLA Aggregate rows into `nhs_drd_discharge_delays`.
- **Credential:** `pipeline_user` on `exempt_pipeline`.

## Table

```sql
nhs_drd_discharge_delays
```

## Natural Key

```
(reporting_period, utla_code)
```

## Query

```sql
INSERT INTO nhs_drd_discharge_delays (
    reporting_period, utla_code, utla_name,
    total_discharges, total_discharges_acceptable_trusts,
    pct_acceptable_trust_coverage, total_bed_days_lost,
    pct_same_day_discharge, pct_delayed_1plus_days,
    discharged_no_delay, discharged_1_day, discharged_2_3_days,
    discharged_4_6_days, discharged_7_13_days,
    discharged_14_20_days, discharged_21_plus_days,
    avg_days_drd_to_discharge_inc_zero,
    avg_days_drd_to_discharge_exc_zero,
    source, loaded_at
) VALUES ($1, $2, ... $19, now())
ON CONFLICT (reporting_period, utla_code) DO UPDATE SET
    utla_name = EXCLUDED.utla_name,
    total_discharges = EXCLUDED.total_discharges,
    ... (all metric columns) ...,
    source = EXCLUDED.source,
    loaded_at = EXCLUDED.loaded_at;
```

## Behaviour

- **Conflict handling:** `ON CONFLICT DO UPDATE` on natural key — re-running with revised data overwrites all columns.
- **Re-run safety:** Fully idempotent. No duplication risk.
- **Source tracking:** Each row stores the download URL of the source file.

## Connection

Docker: `docker exec self-hosted-ai-starter-kit-postgres-1 psql -U pipeline_user -d exempt_pipeline`

## Verified Output

- 3,978 rows (153 UTLAs × 26 months).
- Zero orphans against `utla_lad_mapping`.
- Verified 2024-07-13.
