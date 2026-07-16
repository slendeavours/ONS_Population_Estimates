# Node 5 — Upsert Data

## Type
PostgreSQL DML (INSERT ... ON CONFLICT DO UPDATE)

## Purpose
Load all 296 LA rows into `la_pip_claimants` in a single batch upsert.

## SQL
```sql
INSERT INTO la_pip_claimants (lad24cd, month, pip_total_claimants, pip_enhanced_daily_living)
SELECT r.lad24cd, r.month, r.pip_total_claimants, r.pip_enhanced_daily_living
FROM json_to_recordset($1::json)
    AS r(lad24cd text, month text, pip_total_claimants int, pip_enhanced_daily_living int)
ON CONFLICT (lad24cd, month) DO UPDATE SET
    pip_total_claimants = EXCLUDED.pip_total_claimants,
    pip_enhanced_daily_living = EXCLUDED.pip_enhanced_daily_living,
    loaded_at = NOW();
```

## Logic
1. Serialise all 296 row dicts as a single JSON array
2. Pass to `json_to_recordset` for batch insert
3. On conflict (same LA + month), update values and refresh `loaded_at`
4. No second `pipeline_run_log` entry on re-run (log written once per build, not per upsert)

## Parameters
- Parameterised SQL (`%s` placeholder via psycopg2) — no string concatenation

## Behaviour
- Idempotent: re-running with the same data updates `loaded_at` but does not change row count
- Single transaction, committed after the upsert
- `loaded_at` is server-side `NOW()`, not client-supplied

## Verified Output
- 296 rows upserted
- Idempotency confirmed: re-upsert produced 296→296 rows, 1 log entry
- Verified 2026-07-16 (initial build)
