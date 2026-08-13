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
| Agent name | Source 15 - Land Registry UK HPI |
| Source number | 15 |
| Status | success |
| Target table | pipeline_run_log |

These read `Source 19 - Land Registry UK HPI` and `19` until the July 2026
renumbering moved Land Registry HPI from 19 to 15, freeing 19 for DWP PIP.
The run this node wrote on 2026-07-14 — `pipeline_run_log` id 60 — still
carries the old values and is left alone: the log is an immutable audit
record of what executed at the time, accurate about the run and inaccurate
only against a numbering scheme that changed afterwards. Id 85 is the
backfilled S15 row. See `docs/SOURCE_REGISTRY_GAPS.md` for why id 60's
`source_code` stays null.

## Behaviour
Appends a new row per run. Multiple runs for the same source are expected (monthly refreshes).

## Verified output
Log entry written on 2026-07-14. Edition: April 2026. Period: 2022-01-01 to 2026-04-01. 15,340 rows.
