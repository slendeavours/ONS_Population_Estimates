# Node 6 — Log Run

## Type
PostgreSQL DML (INSERT INTO pipeline_run_log)

## Purpose
Record the build in `pipeline_run_log` with source number, row count, and coverage metadata.

## SQL
```sql
INSERT INTO pipeline_run_log (agent_name, source_number, rows_written, status, notes)
VALUES ($1, $2, $3, $4, $5)
RETURNING id;
```

## Logic
1. Insert one row after a successful upsert
2. `agent_name`: `Source 19 - PIP Claimants`
3. `source_number`: `19`
4. `rows_written`: actual upsert row count
5. `status`: `success`
6. `notes`: month loaded, coverage fraction, confidence rating
7. Capture returned `id` for the verification suite and final report

## Behaviour
- One log entry per build — the idempotency check (Phase 5 check 6) verifies this
- Re-running the upsert does NOT write a second log entry
- Log entry is written in the same transaction as the upsert

## Verified Output
- `pipeline_run_log` entry: id=61
- Notes: "Month: Apr-26. Coverage: 296/296 (100.0%). Confidence: High."
- Verified 2026-07-16 (initial build)
