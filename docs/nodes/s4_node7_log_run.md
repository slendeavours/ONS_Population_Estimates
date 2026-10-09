# S4 Node 7 — Log Run

> **Superseded 2026-10-09.** The n8n S4 workflow this note describes, and the `scripts/verify/rebuild_care_leavers.py` rebuild that followed it (now `scripts/historical/s4_rebuild_care_leavers.py`, which stops with a RETIRED message), are replaced by `scripts/s4_care_leaver_editions.py`, which keeps each release's figures for each year as an edition (`care_leaver_accommodation_editions`) and finds the latest release and both dataset ids itself. The source note is `docs/s4_care_leaver_source.md`. The text below is left as written and describes the old method.

**Type:** Postgres — Execute Query

## Purpose

Writes a completion record to `pipeline_run_log` on successful execution of the S4 workflow.

## Query

```sql
INSERT INTO pipeline_run_log (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
VALUES (
    'Source 4 - DfE Care Leaver Accommodation',
    4,
    $1,
    NOW(),
    NOW(),
    'success',
    'DfE SSDA903 care leaver accommodation by LA: 17-21 (2019-2024) and 22-25 suitability (2023-2025)'
);
```

`$1` = `{{ $('Process CLA Data').first().json.row_count }}`

## Behaviour

- Inserts on every successful run, does not upsert
- `started_at` and `completed_at` are both `NOW()`, so the logged duration is always zero and the row records completion rather than elapsed time

## Connection

- Input: Upsert Data (Node 6)
- Output: none, terminal node

## Verified Output

1 row inserted, `rows_written` = 1413. (2026-03-31)
