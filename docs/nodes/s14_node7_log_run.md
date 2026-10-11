# Node 7 - Log Pipeline Run

> **Superseded 2026-10-11.** This note describes the retired S14 build: `scripts/s14_lha_rates_build.py`, `scripts/s14_lha_rates_build_v2.py` and the n8n workflow "LHA Rates + List of Rents (S14)". Those scripts ran by accident on 2026-08-20 and 2026-10-11 and each time left authorities with no rate row in W1's join; they are now guards that exit 2 and must not be run. S14 is loaded by `scripts/s14_lha_editions.py`, which finds the newest England file in the GOV.UK collection, reads the year from the CSV header, halts on a blank, text or negative rate, skips no row, derives the weekly rates with exact decimals and keeps every file's statement as an edition (`brma_lha_rates_editions`, `la_brma_mapping_editions`). The source note is `docs/s14_lha_source.md`. The text below is left as written and describes the old method.

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
    448,
    NOW(),
    NOW(),
    'success',
    'DWP UC LHA rates 2026-27 (frozen at 2024-25 levels). BRMA-to-LA mapping built via centroid spatial join against VOA BRMA boundaries May 2020. 296 LA mappings + 152 BRMA rates loaded.'
);
```

## Parameters
| Field | Value |
|-------|-------|
| agent_name | 'Source 14 - LHA Rates' |
| source_number | 14 |
| rows_written | 448 (296 LA mappings + 152 BRMA rates) |
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
- Row count: 296 LA mappings + 152 BRMA rates = 448 total rows
- Status: success
