# Node 6 — Log Run

## Type
Python script task

## Purpose
Record the successful ETL run in `pipeline_run_log`.

## Logic
1. Insert a row into `pipeline_run_log` with agent name, source number, row count, timestamps, status, and notes
2. Notes include edition, period range, and applied recodes

## Key parameters
| Parameter | Value |
|---|---|
| Agent name | Source 19 - Land Registry UK HPI |
| Source number | 19 |
| Status | success |
| Target table | pipeline_run_log |

## Behaviour
Appends a new row per run. Multiple runs for the same source are expected (monthly refreshes).

## Verified output
Log entry written on 2026-07-14. Edition: April 2026. Period: 2022-01-01 to 2026-04-01. 15,340 rows.
