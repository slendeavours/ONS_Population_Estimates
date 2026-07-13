# W1 Node 6 Patch: mental_health and learning_disability Tenant Types

## Overview

Adds two new tenant types to `staging_tenant_type_rankings` using MHS26 CRFD days as the primary signal. Both types share the same signal because MHSDS does not disaggregate CRFD by mental health vs learning disability/autism cohort.

## New Tenant Types

| Tenant Type | Primary Signal | Signal Label | Data Confidence |
|---|---|---|---|
| `mental_health` | `crfd_days` | `combined_mh_ld_autism_crfd_days` | Medium |
| `learning_disability` | `crfd_days` | `combined_mh_ld_autism_crfd_days` | Medium |

## SQL

```sql
INSERT INTO staging_tenant_type_rankings (
    run_id, tenant_type, rank_position, lad24cd, la_name,
    primary_signal, signal_label, data_confidence
)
SELECT
    $1,
    'mental_health',
    ROW_NUMBER() OVER (ORDER BY crfd_days DESC NULLS LAST),
    lad24cd, la_name, crfd_days,
    'combined_mh_ld_autism_crfd_days', 'Medium'
FROM staging_la_signals
WHERE run_id = $1 AND crfd_days IS NOT NULL
ON CONFLICT (run_id, tenant_type, rank_position) DO UPDATE SET
    lad24cd = EXCLUDED.lad24cd, la_name = EXCLUDED.la_name,
    primary_signal = EXCLUDED.primary_signal,
    signal_label = EXCLUDED.signal_label,
    data_confidence = EXCLUDED.data_confidence;
```

Repeat with `'learning_disability'` in place of `'mental_health'`.

## Data Confidence: Medium

Reasons:
1. **Combined cohort** — MHS26 covers mental health, learning disability, and autism together. No disaggregation available.
2. **Volume metric** — CRFD days is not population-adjusted; larger LAs naturally rank higher.
3. **Suppression** — 28–46% of LAs have suppressed values per month (91 NULLs in the latest period).

## Run 10 Results

- 205 LAs ranked per tenant type (91 excluded due to suppression).
- Top 5: Birmingham (2,490), Liverpool (1,570), Leeds (1,250), Manchester (1,160), Sefton (790).
- Cohort disaggregation is a known data gap — deferred until a disaggregated source becomes available.
