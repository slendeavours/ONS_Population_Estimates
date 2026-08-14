# S1b Node 6: Log the Run

- **Type:** Postgres INSERT
- **Purpose:** Record the load in `pipeline_run_log` with `source_code` populated, so `vw_source_due` resolves S1b by code rather than by the number fallback.
- **Credential:** `PG_USER` / `PG_PASSWORD` via `scripts/_db.py`.

## Query

```sql
INSERT INTO pipeline_run_log
    (run_id, source_number, source_code, agent_name, status,
     rows_written, notes, started_at, completed_at)
VALUES (gen_random_uuid(), %s, %s, %s, 'success', %s, %s, now(), now());
```

## Query Parameters

| Parameter | Value |
|---|---|
| `source_number` | `1b` |
| `source_code` | `1b` |
| `agent_name` | `Source 1b - MHCLG homelessness support needs (A3)` |
| `status` | `success` |
| `rows_written` | Total rows written this run |
| `notes` | Row count, quarter count, the quarter list, and the target table |

## Constraints worth knowing

- **`agent_name` is `varchar(50)`.** A longer name fails the insert. S23's
  first attempt did, at 58 characters.
- **`status` is constrained to `success` for new writes** by
  `pipeline_run_log_status_new_writes_chk`, added `NOT VALID` so the two
  historical `complete` rows stand. This codifies the existing convention: no
  failure has ever been logged, because a build that fails rolls its
  transaction back and exits non-zero without writing a row.
- **`source_code` has no foreign key** to `source_registry`, deliberately. The
  log is an immutable audit record and historical rows may name sources later
  deprecated.

## Behaviour

- **Conflict handling:** None — the log is append-only. One row per run.
- **Re-run safety:** A re-run appends a second row, which is correct: two runs
  happened.
- **Transaction:** Written inside the same transaction as the data, so a run
  that fails leaves no log row claiming it succeeded. That is the defect
  pattern that made `homelessness_quarter_urls` claim a load of 2025Q1 that
  never landed.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

One row logged: `source_code = '1b'`, `rows_written = 101232`, status
`success`, notes naming all eleven quarters.

Run attribution confirmed by `verify_source_registry.py` gate 6, which checks
parent and sub-source pairs including `1/1b` and found no source resolving to
another's run-log row.

Verified 2026-08-14.
