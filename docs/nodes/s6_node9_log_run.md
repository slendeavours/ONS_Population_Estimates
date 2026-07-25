# Node 9 — Log Run

## Type
Database insert

## Credential
`exempt_pipeline` as `PG_USER` from `.env`.

## Purpose
Write exactly one `pipeline_run_log` row per build, after every verification
check has passed and immediately before the commit.

## Query
```sql
INSERT INTO pipeline_run_log
    (run_id, agent_name, source_number, rows_written, started_at,
     completed_at, status, notes)
VALUES (%s, %s, %s, %s, %s, now(), %s, %s);
```

## Query Parameters

| Parameter | Value |
|---|---|
| `run_id` | UUID generated at start of run |
| `agent_name` | `s6_asylum_build` |
| `source_number` | `6` |
| `rows_written` | Sum across all four data tables |
| `status` | `success` |
| `notes` | Editions loaded, floor date and its reason, per-table row counts, verification result |

## Logic
1. Written **once per build**, not per upsert.
2. Written **after** the verification suite returns, so a logged run is a
   verified run. A failed check rolls back before this node is reached, leaving
   no log row — an absent row means the load did not happen.
3. Committed in the same transaction as the data, so the log and the tables can
   never disagree.

## Behaviour
- `source_number` is `6`. Confirmed free: the column is `varchar` and holds
  `0, 1, 2, 3, 3b, 4, 5, 7, 8, 10, 11, 12, 13, 14, 17, 18, 19, 20, 21, s9a,
  s9b, w1`.
- The floor date and its rationale are recorded in the note, so anyone reading
  the log understands why the series starts at 2018 Q1 without opening the
  source documentation.
- Re-running appends a new log row. Row counts stay constant because the upserts
  are idempotent.

## Connection
Input: verification results from Node 8.
Output: one `pipeline_run_log` row, then `COMMIT`.

## Verified Output
```
source_number : 6
agent_name    : s6_asylum_build
status        : success
rows_written  : 26,936
completed_at  : 2026-07-25 18:09:09 UTC
```

Note recorded:
> S6 Home Office asylum support. Asy_D11 year ending March 2026, Reg_02 year
> ending March 2026. Floor date applied: 2018-01-01 (Section 4 has no LA
> geography before 2018). la_asylum_support 20926, unallocated 84, non_england
> 2374, immigration_groups 3552. Verification: 13 checks, all passed.

Verified 2026-07-25 (initial build)
