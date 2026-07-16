# Source 19 — DWP Personal Independence Payment (PIP) Claimants

| Field | Value |
|---|---|
| Publisher | Department for Work and Pensions (DWP) |
| Database | PIP Cases with Entitlement from 2019 (`str:database:PIP_Monthly_new`) |
| Measure | `str:count:PIP_Monthly_new:V_F_PIP_MONTHLY` (COUNT type) |
| Cadence | Monthly (caseload snapshot; ~2 months lag) |
| API root | `https://stat-xplore.dwp.gov.uk/webapi/rest/v1` |
| Geography | Census 2021 MASTERGEOG21 — all 296 English local authorities (LAD24 codes including 2023 LGR) |
| Join key | `lad24cd` (direct match; no historical-code summing required for current geography) |
| Target table | `la_pip_claimants` (grain: lad24cd × month) |
| Month loaded | Apr-26 |
| Coverage | 296/296 (100.0%) |
| Confidence | High |
| First load | 16 July 2026 (Claude Code build, `pipeline_run_log` id 61) |
| Refresh ID | To load a newer month, change only the latest-month member ID in `discovery.json` — the date valueset currently has 88 periods |

## What it provides

Two demand-proxy columns per LA:

1. **`pip_total_claimants`** — total PIP cases with entitlement. Broad disability-related benefit caseload.
2. **`pip_enhanced_daily_living`** — cases with the Enhanced daily living component. A sharper signal: claimants with substantial daily living needs are the primary HSS-lens demand pool for supported living placements.

National total (Apr-26): 3,710,753 total; enhanced daily living is a subset of this.

## Acquisition pattern

Schema discovery (Node 1) walks the `/schema` endpoint to find every ID programmatically. Table queries (Node 3) use the recodes pattern — explicit member URI maps in the `recodes` object, dimensions referencing field IDs only (including valueset URIs in dimensions causes a DUPLICATE_RECODES error). Batched at 15 LAs per API call to avoid 504 timeouts.

## Rounding and suppression

DWP applies statistical disclosure control. Values below a rounding threshold are published as `..` (nil or negligible). In the loaded data, these appear as `NULL` — absence of a row or a NULL value means no published data, not zero. This is encoded in the `COMMENT ON TABLE`.

## Refresh procedure

1. Re-run `scripts/s19_pip_build.py` — if `s19_cache/discovery.json` exists, Phase 1 is skipped
2. The script automatically selects the latest available month from the date valueset
3. Upsert is idempotent: same month re-loaded updates `loaded_at`, does not duplicate rows

To force a full re-discovery (e.g. if DWP restructures the database), delete `s19_cache/discovery.json` before running.

## Dual-lens note

- **HSS Primary**: disability is the core eligibility criterion for supported living placement demand. PIP enhanced daily living caseload is a direct measure of the population most likely to require supported accommodation. This is the primary demand signal under the HSS lens.
- **UCWS Context**: PIP caseload provides context for the supported-housing operator market — areas with high disability-related benefit volumes indicate a larger addressable demand pool for exempt accommodation providers, but the metric itself does not measure housing need or operator activity.
