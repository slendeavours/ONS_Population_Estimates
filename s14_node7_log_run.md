# Node 7 - Log Pipeline Run

## Type
Postgres INSERT

## Purpose
Record this S14 pipeline execution in pipeline_run_log for audit trail and replay tracking.

## Query

```sql
INSERT INTO pipeline_run_log (agent_name, source_number, rows_written, started_at, completed_at, status, notes)
VALUES (
    'Source 14 - LHA Rates',
    14,
    447,
    NOW(),
    NOW(),
    'success',
    'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping built via centroid spatial join against VOA BRMA boundaries May 2020. 296 LA mappings + 151 BRMA rates loaded.'
);
```

## Parameters
| Field | Value |
|-------|-------|
| agent_name | 'Source 14 - LHA Rates' |
| source_number | 14 |
| rows_written | 447 (296 LA mappings + 151 BRMA rates) |
| started_at | NOW() (query start) |
| completed_at | NOW() (query completion) |
| status | 'success' (only if all prior nodes succeeded) |
| notes | Full context for replay: FY, freeze level, spatial join method |

## Behaviour
- Logged AFTER successful Node 5 & 6 upserts
- Only logged if verification passed (status='success')
- Timestamps are server-time (NOW())
- Notes field includes financial year and method for reproducibility

## Connection
- Input: Completion of Nodes 5 & 6 (all data loaded)
- Output: Single row in pipeline_run_log for audit

## Verified Output
- Pipeline run logged successfully (2026-07-10)
- Row count: 296 LA mappings + 151 BRMA rates = 447 total rows
- Status: success
