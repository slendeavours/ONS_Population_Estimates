# S4 Node 6 — Upsert Care Leaver Accommodation Data

**Type:** Postgres — Execute Query

## Purpose

Upserts all care leaver accommodation rows in a single parameterised batch. Resolves `new_la_code` to `lad24cd` via `la_code_lookup`. `DISTINCT ON` prevents duplicate key violations where the same authority appears under both old and current codes and both resolve to the same `lad24cd`.

## Behaviour

- `DISTINCT ON (c.new_code, reporting_year, age_group)` collapses join fan-out
- `ON CONFLICT DO UPDATE` makes the node idempotent
- `NULLIF(..., 'null')` handles JSON nulls serialised as the string `null` by `JSON.stringify`
- `$1` = `{{ $('Process CLA Data').first().json.batch_json }}`

## County councils were silently dropped

```sql
JOIN la_code_lookup c ON c.old_code = (r->>'new_la_code')
```

This is an inner join. `la_code_lookup` holds **no `E10` entries**, so every county council failed to match and was excluded without warning. DfE publishes 155 upper-tier authorities, 24 of them counties, and the table held 132.

Because care leaver duties sit with upper-tier authorities, counties are among the largest in the dataset: four of the six highest authorities in reporting year 2025 are counties. Every England total and every national rank produced before 2026-08-20 was therefore computed over 132 of 155 authorities and understated the national picture.

Counties are now carried on their own `E10` code. Two consequences follow:

1. They will not join `la_boundaries`, which is a LAD24 district boundary set, so any query that inner-joins boundaries still excludes them.
2. Ranking or mapping that needs to include counties requires an upper-tier geography, which this pipeline does not yet have.

A left join with an explicit unmatched-row count would have surfaced this at build time. Any future geography resolution step should report unmatched rows rather than discarding them.

## Connection

- Input: Create Table (Node 5)
- Output: Log Run (Node 7)

## Verified Output

1413 rows upserted. (2026-03-31)
Rebuilt 2026-08-20: 1087 rows for the 17-21 cohort across 2019–2025, 155 authorities per year.
