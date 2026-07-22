# S19 PIP Claimants — W1 Integration Summary

## What changed

PIP claimant data from `la_pip_claimants` (loaded 16 July 2026, `pipeline_run_log` id 61) was wired into W1 `staging_la_signals` as run 11. Three new columns added to the table schema and populated for all 296 English LAs.

This completes the HSS three-layer package: supply (S11 CQC), demand (S19 PIP), and flow (S9 DRD + CRFD) are now available in a single row per LA in `staging_la_signals`.

## New columns in staging_la_signals

| Column | Type | Source | Description |
|---|---|---|---|
| `pip_total_claimants` | INTEGER | `la_pip_claimants.pip_total_claimants` | Total PIP cases with entitlement |
| `pip_enhanced_daily_living` | INTEGER | `la_pip_claimants.pip_enhanced_daily_living` | Enhanced daily living component subset |
| `pip_rate_per_1000` | NUMERIC(8,2) | Derived: total / population * 1000 | Normalised demand rate per 1,000 population |

## Verification results (run 11)

| Check | Result | Detail |
|---|---|---|
| V1 Row count | PASS | 296/296 |
| V2 PIP coverage | PASS | 296/296 all three columns |
| V3 Top 5 rates | PASS | Knowsley 129.24, Blackpool 120.38, Hartlepool 118.22, Liverpool 116.41, Sunderland 109.64 |
| V4 Priority markets | PASS | 8/8 — Liverpool, Hull, Nottingham, Bradford, Sheffield, Derby, Coventry, Leicester |
| V5 National total | PASS | 3,710,753 (exact match to standalone load) |
| V6 TA intact | PASS | 294 non-null (inherited: Barnsley + Sheffield NULL in run 10 too) |
| V7 S9 intact | PASS | DRD 296/296, CRFD 205/296 (identical to run 10) |

## Run number

- **W1 run:** 11
- **pipeline_run_log id:** 64
- **PIP month:** Apr-26
- **Method:** Copy-from-run-10 with PIP LEFT JOIN (n8n API not accessible for direct Node 5 update)

## n8n permanent update

The patch specification for updating the live n8n Node 5 query is in `docs/nodes/s19_w1_node5_pip_patch.md`. Apply it when n8n API access is restored — adds the JOIN, SELECT columns, and ON CONFLICT SET clauses to the permanent workflow.
