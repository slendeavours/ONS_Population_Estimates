# Node 6 - Deactivation sweep

## Type
Postgres update, one operation (`scripts/s11_cqc_load.py`, node 6 step)

## Purpose
Preserve the deregistration signal. The monthly file contains only currently active locations, so a location absent from it has deregistered (or CQC's migration is delaying its record; see the caveat in the table comment). Rather than deleting, the sweep marks such rows inactive and stamps when the pipeline first noticed. S11 is the pipeline's only supply-side source, and supply contraction over time is part of what it exists to record.

## Query
```sql
UPDATE cqc_locations
SET is_active = FALSE, deregistered_seen_date = %s
WHERE is_active = TRUE AND source_file_date < %s
```

## Query Parameters
| Parameter | Value |
|---|---|
| 1 | current file date (deregistered_seen_date stamp) |
| 2 | current file date (cut-off) |

## Behaviour
Node 5 sets `source_file_date` to the current file date on every row present in the file, so rows with an older date are exactly the absent ones. The `is_active = TRUE` guard means a row is stamped once: later sweeps skip already-inactive rows and the original sighting date survives. Re-running the same file deactivates nothing, and a location reappearing in a later file is reactivated by Node 5's conflict clause.

## Connection
- Input: Node 5 - Upsert locations
- Output: Node 7 - Log run

## Verified Output (2026-07-12)
First load: 0 rows deactivated (nothing predates the 2026-07-01 file). Idempotency re-run: 0 rows deactivated, counts stable at 30,492 total, 30,492 active.
