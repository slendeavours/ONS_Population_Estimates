# Node 7 - Log run

## Type
Postgres insert, one operation (`scripts/s11_cqc_load.py`, node 7 step)

## Purpose
Record the load in `pipeline_run_log`, the shared audit table every source writes to.

## Query
```sql
INSERT INTO pipeline_run_log (run_id, agent_name, source_number, status,
                              rows_written, started_at, completed_at,
                              duration_ms, notes)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
```

## Query Parameters
| Parameter | Value |
|---|---|
| run_id | fresh UUID per run |
| agent_name | `Source 11 - CQC Care Providers` |
| source_number | `11` |
| status | `success` (the script aborts before this node on failure) |
| rows_written | upserted row count |
| started_at / completed_at / duration_ms | run timestamps, UTC |
| notes | `CQC Care directory with filters, file date <date>; ASC directorate scope; upsert with deactivation` |

## Behaviour
One insert per run. Re-runs insert a new log row each, which is the intended audit trail, not duplication.

## Connection
- Input: Node 6 - Deactivation sweep
- Output: none (terminal node)

## Verified Output (2026-07-12)
Two success rows logged (initial load and idempotency re-run), rows_written 30,492, notes carrying file date 2026-07-01.
