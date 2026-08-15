# S1b Node 4: Extract A3 Rows and Upsert

- **Type:** Code + Postgres upsert
- **Purpose:** Turn each mapped column of each local authority row into one long-format row, resolving geography and preserving suppression, then upsert.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Logic

1. **Select local authority rows.** Column 0 codes of length 9 beginning `E`
   with prefix `06`, `07`, `08` or `09`. This excludes `E92000001` (England)
   and `E12*` (regions) deliberately — those rows are weighted to impute for
   non-submitting authorities, so they are not the sum of the LA rows and must
   not be loaded as areas.

2. **Resolve geography.** Every publisher code goes through `la_code_lookup`:

   ```sql
   SELECT old_code, new_code FROM la_code_lookup WHERE old_code = ANY(%s)
   ```

   Any code that does not resolve halts the build with the list. Quarters to
   2024Q4 carry `E08000016` / `E08000019` and 2025Q1 onward carry
   `E08000038` / `E08000039`; both resolve to the canonical `E08000016` and
   `E08000019`, so Barnsley and Sheffield are one series across all eleven
   quarters. S1 does not do this and splits both.

3. **Read each cell** into a `(value, flag)` pair where exactly one is
   populated:

   ```python
   FLAGS = {"..": "missing", "-": "suppressed",
            "[x]": "missing", "[c]": "suppressed", "[z]": "not_applicable"}
   ```

   A blank cell is `missing`. A numeric cell is the integer with a null flag.
   **Anything else halts the build** — an unrecognised marker is neither a
   number nor a documented symbol, and guessing would defeat the point of the
   node.

4. **Emit one row per (authority, mapped column)** carrying the resolved
   `lad24cd`, the publisher code, the canonical `category_code`, the
   publisher's own header text as `category_label`, and the full provenance
   set from node 1.

## Query

```sql
INSERT INTO la_homelessness_support_needs (
    lad24cd, period, category_code, value, value_flag, category_group,
    category_label, reference_quarter, source_url, source_edition,
    edition_variant, release_page_url, layout_version, publisher_la_code)
VALUES %s
ON CONFLICT (lad24cd, period, category_code) DO UPDATE SET
    value             = EXCLUDED.value,
    value_flag        = EXCLUDED.value_flag,
    category_group    = EXCLUDED.category_group,
    category_label    = EXCLUDED.category_label,
    reference_quarter = EXCLUDED.reference_quarter,
    source_url        = EXCLUDED.source_url,
    source_edition    = EXCLUDED.source_edition,
    edition_variant   = EXCLUDED.edition_variant,
    release_page_url  = EXCLUDED.release_page_url,
    layout_version    = EXCLUDED.layout_version,
    publisher_la_code = EXCLUDED.publisher_la_code,
    loaded_at         = now();
```

Executed with `psycopg2.extras.execute_values`, page size 1000. Parameterised
throughout; no value is concatenated into SQL.

## Query Parameters

| Parameter | Source |
|---|---|
| `period` | The pipeline's financial-year quarter key |
| `reference_quarter` | Publisher's calendar quarter end, `YYYY-MM` |
| `source_url`, `source_edition`, `edition_variant`, `release_page_url` | Node 1 |
| `layout_version` | Node 2 — `legacy_37col` or `v2026_34col` |
| `category_code`, `category_group` | Node 2 |
| `category_label` | The publisher's own header text for that column, this edition |

## Why `edition_variant` is a stored column rather than a note

A revising source needs to answer "which file produced this figure" from the
row, not from a side table. `homelessness_quarter_urls` is the same idea at
coarser grain and it has disagreed with the data in both directions — one
quarter marked loaded with no rows, one loaded with no register entry. A
column on the row cannot disagree with the row.

## Behaviour

- **Conflict handling:** Upsert on the natural key. Re-running a quarter
  replaces its values in place, which is correct for a source that revises.
- **Re-run safety:** Idempotent. Gate 6 re-upserts all 101,232 rows inside a
  rolled-back transaction and compares a content checksum either side.
- **Failure:** The whole run is one transaction. Any halt rolls back
  everything, so a partial quarter cannot land.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

- 101,232 rows across eleven quarters: 9,176 per quarter for the ten legacy
  quarters (296 × 31) and 9,472 for 2025Q4 (296 × 32).
- 296 distinct authorities in every quarter.
- 1,620 `missing`, 772 `suppressed`, 18,724 genuine zeros, 0 coerced.
- Middlesbrough 2025Q2 `mental_health_history` = 297, against 6 in S1's
  mis-mapped column.
- Verified 2026-08-14.
