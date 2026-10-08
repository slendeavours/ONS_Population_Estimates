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
| Target table | `la_pip_claimants` (latest edition; grain: lad24cd × month) and `la_pip_claimants_editions` (every release; key lad24cd × month × edition) |
| Month key | `yyyymm` text (`202607`). Until 2026-10-08 text labels such as `Apr-26` (`Apr-26` became `202604`, `Jul-26` became `202607`) |
| Months held | 202604 to 202607 (four months, 296/296 authorities each, edition 1) |
| Latest month at the API | 202607 on 2026-10-08 (the API offered 201901 to 202607) |
| First load | 16 July 2026 (Claude Code build, `pipeline_run_log` id 61); moved onto the editions loader 2026-10-08 |
| Loader | `scripts/s19_pip_editions.py`; verify `scripts/s19_pip_editions_verify.py` |

## What it provides

Two demand-proxy columns per LA:

1. **`pip_total_claimants`** — total PIP cases with entitlement. Broad disability-related benefit caseload.
2. **`pip_enhanced_daily_living`** — cases with the Enhanced daily living component. A sharper signal: claimants with substantial daily living needs are the primary HSS-lens demand pool for supported living placements.

National totals held (England, 296 authorities): 202604 3,708,965 total and 1,944,596 enhanced daily living; 202607 3,788,643 and 1,985,494. Enhanced daily living is a subset of the total. (The first-load note recorded 3,710,753 for April; the held April sum is 3,708,965 and the difference has not been investigated.)

## Acquisition pattern

Schema discovery (Node 1) walks the `/schema` endpoint to find every ID programmatically. Table queries (Node 3) use the recodes pattern — explicit member URI maps in the `recodes` object, dimensions referencing field IDs only (including valueset URIs in dimensions causes a DUPLICATE_RECODES error). Batched at 15 LAs per API call to avoid 504 timeouts.

## Rounding and suppression

DWP applies statistical disclosure control. Values below a rounding threshold are published as `..` (nil or negligible). In the loaded data, these appear as `NULL` — absence of a row or a NULL value means no published data, not zero. This is encoded in the `COMMENT ON TABLE`.

## Refresh procedure

Monthly, with `scripts/s19_pip_editions.py` (full steps in `docs/QUARTERLY_REFRESH.md`, section "S19 PIP claimants"):

1. `python scripts/s19_pip_editions.py load` previews. The loader asks the API which months exist, fetches new months and rechecks the latest six held ones. `--recheck-all` rechecks every held month.
2. `load --commit` stores a new month as edition 1 and its live rows in one transaction; a revised month becomes the next edition and reaches the live table through `refresh-latest --commit`.
3. A first `sync-new` needs `--expected-authorities 296`.

The old loader `scripts/s19_pip_build.py` (cached discovery, upsert in place, text month labels) is archived in `scripts/historical/` and must not be run.

## Revision status

Not established. 202604 and 202607 were rechecked once, a week after their first load (2026-10-08), and no authority differed. That is a short test: it does not show that PIP is never revised (HB, from the same Stat-Xplore account, was revised on 285 of 296 authorities with no note). The loader rechecks the latest six months on every load, so a longer run of monthly checks will settle it.

## Dual-lens note

- **HSS Primary**: disability is the core eligibility criterion for supported living placement demand. PIP enhanced daily living caseload is a direct measure of the population most likely to require supported accommodation. This is the primary demand signal under the HSS lens.
- **UCWS Context**: PIP caseload provides context for the supported-housing operator market — areas with high disability-related benefit volumes indicate a larger addressable demand pool for exempt accommodation providers, but the metric itself does not measure housing need or operator activity.
