# S23 Node 6: Log the Run

- **Type:** Postgres INSERT
- **Purpose:** Record the load in `pipeline_run_log` with `source_code` populated so `vw_source_due` resolves S23 by code.
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
| `source_number` | `23` |
| `source_code` | `23` |
| `agent_name` | `Source 23 - RSH RP stock by local authority` |
| `status` | `success` |
| `rows_written` | Provider rows written |
| `notes` | Edition, stock date, publication date, target table |

**`agent_name` is `varchar(50)`.** The first draft of this node used
"Source 23 - RSH registered provider stock by local authority" at 58
characters and the insert failed with `StringDataRightTruncation`. The name
above is 42.

## Behaviour

- **Conflict handling:** None — append-only, one row per run.
- **Re-run safety:** A re-run appends a second row, which is correct.
- **Transaction:** Written inside the same transaction as the data, so no log
  row can claim a load that did not land.

## Connection

Postgres `exempt_pipeline` on `localhost:5432`.

## Verified Output

One row logged: `source_code = '23'`, `rows_written = 10171`, status
`success`, notes recording edition "2024 to 2025", stock date 2025-03-31,
publication date 2025-10-28.

Verified 2026-08-14.
