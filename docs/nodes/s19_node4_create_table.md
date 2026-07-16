# Node 4 — Create Table

## Type
PostgreSQL DDL

## Purpose
Create `la_pip_claimants` if it does not exist, with table and column comments encoding provenance, coverage, confidence, and caveats.

## SQL
```sql
CREATE TABLE IF NOT EXISTS la_pip_claimants (
    lad24cd text NOT NULL,
    month text NOT NULL,
    pip_total_claimants integer,
    pip_enhanced_daily_living integer,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lad24cd, month)
);
ALTER TABLE la_pip_claimants OWNER TO pipeline_user;
```

## Logic
1. Create table with composite primary key `(lad24cd, month)`
2. Set ownership to `pipeline_user`
3. Apply `COMMENT ON TABLE` with database ID, month loaded, coverage, and confidence
4. Apply `COMMENT ON COLUMN` for both measure columns:
   - `pip_total_claimants`: total PIP cases with entitlement, plus DWP rounding annotation and any historical-code summing note
   - `pip_enhanced_daily_living`: enhanced daily living award — noted as the sharper HSS demand signal (disability is the core eligibility criterion for supported living placement demand)

## Parameters
- Grain: one row per LA per month
- Both measure columns are nullable (absence means no published data, not zero)

## Behaviour
- Idempotent: `IF NOT EXISTS` means safe to re-run
- Column comments persist across re-runs
- No data migration — table shape is fixed

## Connection
References: `la_boundaries.lad24cd` (integrity check in Phase 5, not enforced as FK)

## Verified Output
- Table created successfully
- Comments applied with database ID `str:database:PIP_Monthly_new`, month Apr-26, coverage 296/296 (100.0%), confidence High
- Verified 2026-07-16 (initial build)
