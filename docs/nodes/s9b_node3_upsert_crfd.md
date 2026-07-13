# S9b Node 3: Upsert MHS26 CRFD

- **Type:** SQL (Postgres)
- **Purpose:** Idempotent upsert of parsed MHS26 rows into `nhs_mh_crfd`.
- **Credential:** `pipeline_user` on `exempt_pipeline`.

## Table

```sql
nhs_mh_crfd
```

## Natural Key

```
(reporting_period, lad24cd, measure_id)
```

## Query

```sql
INSERT INTO nhs_mh_crfd (
    reporting_period, lad24cd, la_name, measure_id, measure_name,
    measure_value, source, loaded_at
) VALUES ($1, $2, $3, $4, $5, $6, $7, now())
ON CONFLICT (reporting_period, lad24cd, measure_id) DO UPDATE SET
    la_name = EXCLUDED.la_name,
    measure_name = EXCLUDED.measure_name,
    measure_value = EXCLUDED.measure_value,
    source = EXCLUDED.source,
    loaded_at = EXCLUDED.loaded_at;
```

## Behaviour

- **Conflict handling:** `ON CONFLICT DO UPDATE` on natural key.
- **Re-run safety:** Fully idempotent.
- **Source tracking:** Each row stores the download URL of the source file.

## Connection

Docker: `docker exec self-hosted-ai-starter-kit-postgres-1 psql -U pipeline_user -d exempt_pipeline`

## Verified Output

- 11,248 rows (296 LAs × 38 months).
- 298 distinct LA codes across all periods (296 base + 2 Barnsley/Sheffield recodes).
- Verified 2024-07-13.
