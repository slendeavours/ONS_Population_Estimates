# Node 7 — Upsert Data

## Type
Batched parameterised upsert inside a single transaction

## Credential
`exempt_pipeline` as `PG_USER` from `.env`.

## Query
```sql
INSERT INTO la_asylum_support
    (period_ending, lad24cd, published_la_name, support_type,
     accommodation_type, people, source_marker, source_edition, loaded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (period_ending, lad24cd, support_type, accommodation_type)
DO UPDATE SET published_la_name = EXCLUDED.published_la_name,
              people            = EXCLUDED.people,
              source_marker     = EXCLUDED.source_marker,
              source_edition    = EXCLUDED.source_edition,
              loaded_at         = now();
```

The other three tables follow the same shape on their own natural keys.
`asylum_series_breaks` is `DELETE` then `INSERT`, which is idempotent for a
two-row reference table and avoids a contrived unique constraint over a nullable
column.

## Logic
1. Upsert `la_asylum_support` — 20,926 rows.
2. Upsert `la_asylum_support_unallocated` — 84 rows.
3. Upsert `asylum_support_non_england` — 2,374 rows.
4. Upsert `la_immigration_groups` — 3,552 rows.
5. Replace `asylum_series_breaks` — 2 rows.

All five run inside one transaction, opened before Node 6's DDL and committed
only after Node 8 reports every check passing.

## Query Parameters

| Parameter | Value |
|---|---|
| `page_size` | 1,000 |
| Placeholder style | `%s` positional, `%(name)s` named for Reg_02 |
| Transaction | single, `autocommit = False` |

## Behaviour
- **Idempotent.** Re-running produces identical row counts and an identical
  checksum over `(period_ending, lad24cd, support_type, accommodation_type,
  people)`. Only `loaded_at` changes. Check 7 proves this by running the whole
  upsert a second time within the same run.
- **Full replace of the periods covered.** The Home Office revises historical
  periods — accommodation type in June 2024, geographic distribution in August
  2024, accommodation types again in November 2025 — so `DO UPDATE` overwrites
  rather than skipping. Prior periods are not immutable.
- **Rows are pre-aggregated by Node 4.** `ON CONFLICT DO UPDATE` on a colliding
  set would keep one row and discard the rest; summing happens before the
  database sees them.
- **Parameterised throughout.** No string concatenation into SQL anywhere.
- Any exception, or any failed verification check, rolls back all five tables
  together. Partial data is never left behind.

## Connection
Input: parsed datasets from Nodes 4 and 5.
Output: populated tables for Node 8.

## Verified Output

| Table | Rows | People |
|---|---:|---:|
| `la_asylum_support` | 20,926 | 2,164,730 |
| `la_asylum_support_unallocated` | 84 | 225,515 |
| `asylum_support_non_england` | 2,374 | 330,601 |
| `la_immigration_groups` | 3,552 | 615,954 |
| `asylum_series_breaks` | 2 | — |

- Idempotency checksum stable across two loads: `667f97f0a47bc2090dc55190a1d1c377`
- Verified 2026-07-25 (initial build)
