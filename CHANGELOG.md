# Changelog

All notable changes to this project are documented here.
Format follows Keep a Changelog. Versioning follows semver where tags are used.

## [Unreleased]

### Security
- **A database credential was supplied as a fallback default in `s15_hpi_build.py` and has been removed. The credential has been rotated.** The value is not restated here or anywhere else in the repository. It had been present across 25 commits under two filenames, and the account it belonged to is a Postgres superuser owning four databases, so the exposure was cluster-wide rather than limited to `exempt_pipeline`.
- Every build script now resolves `PG_USER` and `PG_PASSWORD` through a `_require_env` helper that stops with a clear error rather than falling back to a literal. Host, port and database name keep defaults; they are addressing, not credentials. `scripts/s11_cqc_load.py` and `scripts/s18_pipr_load.py` already behaved this way and are unchanged.
- Hardcoded absolute `.env` path removed from `scripts/s8b_hb_accom_type_build.py`. It resolved relative to a specific machine and leaked a local username.
- `.gitleaks.toml` added. **The default gitleaks ruleset does not catch this class of leak** — verified, it reports "no leaks found" against the history containing the live credential, because its rules target high-entropy secrets and this was a short dictionary word. Four custom rules close the gap: credential defaults in `os.getenv`/`environ.get`, inline database credential literals, connection URIs with embedded credentials, and hardcoded local paths.
- History has **not** been rewritten. Rotation makes the exposed value inert, and rewriting published history would break every existing clone and fork without recovering a secret that must be assumed compromised regardless.
- Decision record: `docs/decisions/2026-07-25-credential-default-exposure.md`.

### Added
- Source 6 (Home Office asylum support by local authority). Two tables from the quarterly immigration system statistics release: `Asy_D11` as a time series and `Reg_02` as a single snapshot. Loaded into `la_asylum_support` (20,926 rows, 33 quarters from 2018 Q1), `la_asylum_support_unallocated` (84 rows), `asylum_support_non_england` (2,374 rows) and `la_immigration_groups` (3,552 rows, 296 LAs × 12 pathway rows).
- `vw_la_asylum_support_totals` — one row per (period, LA) with the accommodation and support-type split, including `accommodation_not_stated` so the breakdown columns sum to the total.
- `asylum_series_breaks` — machine-readable record of the two structural breaks, so consumers of the view see them without reading documentation.
- Build script `s6_asylum_build.py` and verification suite `s6_asylum_verify.py`: landing-page discovery, download, parse, code-first geography resolution, SUM aggregation on the natural key, single-transaction upsert, 13 halting checks, run logging.
- Node documentation `docs/nodes/s6_node1..9*.md`, source summary `docs/s6_asylum_source.md`, build summary `docs/S6_BUILD_SUMMARY.md`, and generated anomalies record `docs/s6_source_anomalies.md`.
- Decision record `docs/decisions/2026-07-25-la-code-lookup-cumbria-off-by-one.md`.
- `docs/METHODOLOGY.md`: source register rows for **S8b** (DWP Stat-Xplore HB accommodation type, built 2026-07-22) and **S15** (Land Registry UK HPI, built 2026-07-14), neither of which had been added when those sources landed. Their tables `la_hb_accom_type_caseload` and `la_house_prices` added to the architecture listing.
- `docs/README.md`: `s15_hpi_build.py` and `s15_hpi_source.md` added to the repository structure.

### Changed
- `docs/METHODOLOGY.md`: S6 added to the source register in source-number order, marked standalone; S6 caveats and geography dependency recorded; pipeline-wide standing rule on unresolved codes added.
- `docs/README.md`: S6 scripts and documents added to the repository structure; review stamp updated to 2026-07-25.

### Fixed
- **`la_code_lookup` contains a confirmed error.** `E07000027` (Barrow-in-Furness) maps to `E06000063` Cumberland; the correct successor is `E06000064` Westmorland and Furness. `E07000028` (Carlisle) and `E07000189` (South Somerset) have no row at all. Verified against the ONS area pages for E06000063, E06000064 and E06000066 plus the Cumbria and Somerset (Structural Changes) Orders 2022. S6 works around this in a build-local resolution layer and does **not** write to the shared table; the repair is tracked as a separate remediation task.
- This supersedes the note in the 2026-07-22 entry below, which recorded `E07000028` and `E07000189` as "extinct LAs with no successor in `la_code_lookup`" and treated them as harmless. They are not extinct: both have successors, and the reason they were missing was a transcription error that also misrouted a third code. That mis-classification is why the pipeline now has a standing rule that unresolved codes are reported as UNEXPLAINED, never as harmless, benign or expected.
- **Source 15 renumbering completed.** `s15_hpi_build.py` still wrote `source_number = 19` to `pipeline_run_log`, so the collision with S19 (DWP PIP) that the renumbering was meant to resolve was still live under a filename that said otherwise. The script's docstring, temp directory, run-log `agent_name` and `source_number` now all read 15, and `s15_hpi_source.md` names the correct refresh script. `index.html` carries a one-line comment change only — no layer definition, legend rule, field name or data source URL is affected, so nothing the map renders changes.

### Notes
- **S6 is standalone.** No Workflow 1 integration, no `staging_la_signals` column, no tenant type, no map layer, no composite index — the same pattern as S19 PIP. Its data is not exported to this repository; it lives only in Postgres.
- **Loaded from 2018 Q1, not the full 2014 series.** Section 4 carries no LA geography before 2018, so earlier quarters cannot be aggregated consistently across support types.
- **Two structural breaks make England totals non-comparable before 2025-03-31.** Section 98 gained LA geography at 2022-12-31, so England rises from 53,749 to 98,375 as a reporting change rather than arrivals. Subsistence Only lost LA geography for five quarters to 2024-12-31, which accounts for most of the apparent swing in LA coverage.
- **Zeros are never published**, so an absent LA means "not published" rather than "none". Confirmed independently by the distribution check: 344 present − 164 under 100 = 180 at or above, against a published 181 of 361 under 100 implying 180.
- Figures use the person's registered address, not necessarily where they reside, and exclude unaccompanied asylum-seeking children. S6 is not a count of all asylum seekers in an area.
- `Asy_D09` is downloaded at verification time as an independent per-period reference and is never loaded into a table.
- **Until the `la_code_lookup` remediation lands, the database is inconsistent across sources on Cumberland (E06000063) and Westmorland and Furness (E06000064), and S6 is the only correct one.**
- The S8 register row still reads "Housing Benefit asylum seeker caseload" and is deliberately unchanged here. Correcting it belongs with the map layer relabel, not with the renumbering.

## [2026-07-22]
### Added
- Source 8b (DWP HB Accommodation Type Breakdown): `la_hb_accom_type_caseload` table with SA, TA, OTHER, UNKNOWN categories across 296 English LAs, 6 months (202509-202602). National SA total ~230k, TA ~112k. Schema discovered from Stat-Xplore REST API.
- Build script `scripts/s8b_hb_accom_type_build.py` — full pipeline: metadata discovery, batched table queries (50 LAs/batch), geography resolution via `la_code_lookup`, upsert via `json_to_recordset`, verification suite (coverage, boundary, anchor, consistency, reasonableness).
- New map layer: "HB Specified Accommodation" choropleth (mid-blue sequential ramp), inserted below "TA Households" in the layer list. Driven by `hb_sa_claimants_latest` in the signals JSON.
- `hb_sa_claimants_latest` field added to `staging_la_signals_latest.json` (296/296 LAs, latest month Feb-26).

### Changed
- W1 run 11: `staging_la_signals` gains `pip_total_claimants`, `pip_enhanced_daily_living`, `pip_rate_per_1000` — wired from `la_pip_claimants` (S19, Apr-26). Completes the HSS three-layer package (S11 supply + S19 demand + S9 flow) in a single row per LA.
- `docs/DATA_DICTIONARY.md`: `hb_sa_claimants_latest` added to Housing Benefit section.
- `docs/README.md`: HB Accommodation Type row added to data table; S8b script added to repo structure; review stamp updated to 2026-07-22.

### Notes
- Stat-Xplore returns monthly granularity for the accommodation type breakdown, not quarterly as the DWP release note implied. Refresh cadence recorded as monthly.
- The existing S8 table `la_hb_sa_caseload` (filtered to C_SATA:1) is unchanged. S8b stores the full breakdown in a separate table.
- Two historical geography codes (E07000028, E07000189) were unresolvable — these are extinct LAs with no successor in `la_code_lookup`. All 296 current LAD24CD codes are covered.
- Birmingham SA consistency check: 9.6% difference vs `la_hb_sa_caseload` for Nov-25 — within the 10% threshold, attributable to retrospective revisions.

## [2026-07-16]
### Added
- Source 19 (DWP PIP Claimants): `la_pip_claimants` table with `pip_total_claimants` and `pip_enhanced_daily_living` columns, 296 English LAs, month Apr-26. National total 3,710,753. Schema discovered programmatically from Stat-Xplore REST API `/schema` endpoint.
- Build script `scripts/s19_pip_build.py` — full pipeline: schema discovery with checkpoint caching, geography resolution, batched table queries (15 LAs/batch, exponential backoff on 504), upsert via `json_to_recordset`, 6-check verification suite.
- Representative query bodies: `s19_query_total.json`, `s19_query_enhanced_dl.json`.
- Node documentation: `docs/nodes/s19_node1..6*.md`.
- Source summary: `docs/s19_pip_source.md` (publisher, cadence, coverage, dual-lens note, refresh procedure).

### Changed
- `docs/METHODOLOGY.md`: S19 (DWP PIP) added to source register; `la_pip_claimants` added to architecture table list.
- `docs/README.md`: PIP Claimants row added to data table; S19 script and docs added to repo structure; review stamp updated.
- `.gitignore`: `s19_cache/` added (Stat-Xplore API response cache, local only).

### Notes
- DWP applies statistical disclosure control: values below rounding threshold appear as NULL (not zero).
- Stat-Xplore `/schema` paginates valueset members at 100; the build script follows `Link: rel="next"` headers automatically.
- Enhanced daily living is the sharper HSS-lens demand proxy (disability as core eligibility criterion for supported living). Total caseload is the broader measure.
- PIP caseload is not yet wired into Workflow 1 or the demand map. W1 integration and map visualisation are explicitly out of scope for this build.

## [2026-07-13]
### Added
- Source 9a (NHS DRD monthly discharge delays): 26 months (Apr 2024 – May 2026) loaded into `nhs_drd_discharge_delays` (3,978 rows, 153 UTLAs). UTLA→LAD apportionment via population-weighted `utla_lad_mapping` (296 rows) and `vw_drd_discharge_delays_lad` view.
- Source 9b (MHSDS MHS26 CRFD): 38 months (Apr 2023 – May 2026) loaded into `nhs_mh_crfd` (11,248 rows, 296 LAs). Barnsley/Sheffield code transition (E08000016→E08000038, E08000019→E08000039 from June 2025) handled via `la_code_lookup` and `vw_mh_crfd_lad` view.
- `staging_la_signals` gains three columns: `drd_bed_days_lost` (296/296), `drd_pct_delayed_1plus_days` (296/296), `crfd_days` (205/296 — 91 suppressed at source).
- Two new tenant types in `staging_tenant_type_rankings`: `mental_health` and `learning_disability`, both ranked by MHS26 CRFD days (205 LAs each). Both share the same signal — MHSDS does not disaggregate by cohort at sub-national level.
- W1 re-run as run 10 with all S9 columns populated. Seven tenant types now active.
- Node documentation: `docs/nodes/s9a_node1..3*.md`, `docs/nodes/s9b_node1..3*.md`, `docs/nodes/s9_utla_lad_mapping.md`, `docs/nodes/s9_w1_node5_patch.md`, `docs/nodes/s9_w1_node6_tenant_types.md`.
- Build summary: `docs/S9_BUILD_SUMMARY.md`.

### Changed
- `docs/DATA_DICTIONARY.md`: added Care Providers (Supply Side) section (`supported_living_locations`) and Discharge Delays section (`drd_bed_days_lost`, `drd_pct_delayed_1plus_days`, `crfd_days`).
- `docs/METHODOLOGY.md`: S9a and S9b added to source register; `nhs_drd_discharge_delays`, `nhs_mh_crfd`, `utla_lad_mapping` added to architecture table list; stale "NHS integration pending" gap replaced with three specific S9 limitations (CRFD disaggregation, DRD apportionment resolution, CRFD suppression rate).
- `docs/README.md`: Discharge Delays and CRFD rows added to data table; `S9_BUILD_SUMMARY.md` added to repo structure; review stamp updated.

### Notes
- CRFD cohort disaggregation is a known data gap — both `mental_health` and `learning_disability` tenant types share the same MHS26 signal. Deferred until a disaggregated source is available.
- DRD percentage columns are UTLA-level pass-through for county districts (all E07 districts under an E10 county inherit the same value).
- Monthly refresh not yet scheduled in n8n. Node 9 GeoJSON export query needs patching to include the three new columns.

## [2026-07-12]
### Added
- Demand map: "Care Providers (SL)" layer — active, non-dormant CQC supported living locations per LA, cream-to-amber quantile ramp, sidebar entry separated as the only supply-side layer, "Care Supply (CQC)" section in the detail panel, legend ends None/High. Dual-registered locations count twice by design (both registrations are regulated entities).
- W1 pre-computation now carries S11 counts: `staging_la_signals` gains `supported_living_locations` (active, non-dormant CQC supported-living locations per LA), W1 Node 5 joins `cqc_locations`, and the Node 9 export SQL in `n8n/workflow_nodes.json` includes the new column. W1 re-run completed (run 9); map data files (`data/boundaries/la_boundaries.geojson`, `data/signals/*.json`) re-exported from it.
- Source 11 (CQC registered care providers), the pipeline's only supply-side source: ETL scripts under `scripts/` (fetch with streaming ODS conversion, process, spatial LA mapping, load, verify) loading `cqc_locations` in the pipeline database - 30,492 Adult social care locations from the 1 July 2026 Care directory with filters, with supported living, personal care, care home and service-user-band flags for the dual UCWS/HSS lens.
- Refresh model: upsert on location ID with a deactivation sweep, so locations dropping off the monthly register keep a `deregistered_seen_date` instead of being deleted (supply-contraction signal).
- Node documentation `docs/nodes/s11_node1..7*.md` and decision record `docs/decisions/2026-07-12-s11-cqc-la-mapping-method.md` (the file's LA column is upper-tier only; mapping is spatial).
- Processed dataset `data/processed/cqc_locations_mapped.csv` (30,492 rows).

### Changed
- `docs/METHODOLOGY.md`: source register renumbered to match `pipeline_run_log.source_number`, the authoritative numbering (the table's own row numbers had drifted); S11 and Census tenure (3b) rows added; EFS and S.114 rows merged into source 12 (LA financial stress); `cqc_locations` added to the architecture table list.
- `docs/README.md`: repository structure updated for the S11 scripts and `docs/decisions/`; review stamp updated.

### Notes
- CQC is migrating its directory to a new digital system (their file README, July 2026): deregistrations can appear late and multi-service locations show as Not Rated. The monthly refresh should re-verify page and file layout each run.
- Five locations (0.016%) were excluded from the July 2026 load because their postcodes are unknown to ONSPD; they are listed in the Node 3 doc and will resolve on a later run.

## [2026-07-11]
### Added
- Source 18 (ONS Price Index of Private Rents): backfill ETL scripts under `scripts/` (fetch, inspect, transform, load, verify) loading `la_private_rents` in the pipeline database, periods 2024-03 onward, England LAs, bedroom and property-type breakdowns.
- Geography dimension tables `la_geography` (seeded from the ONS Code History Database, June 2026) and `la_succession` (seeded from `la_code_lookup` historical mappings) ahead of the 2027/2028 LGR wave.
- Processed dataset `data/processed/la_private_rents_17june2026.csv` (71,442 rows).
- Documentation: `docs/s18_pipr_source.md`, `docs/s18_pipr_workbook_structure.md` (n8n S18 build spec), `docs/geography_dimension.md`.
- Repository `.gitignore` and this changelog (repo predates both).

### Changed
- `docs/METHODOLOGY.md`: data-source register gains S14 (LHA rates, previously missing) and S18 (PIPR); database table list updated with the S14 and S18 tables.
- `docs/README.md`: repository structure updated for `scripts/` and new docs; machine-readable metadata block added.

### Notes
- Raw ONS downloads (`data/raw/`) are not committed — re-fetchable via `scripts/s18_pipr_fetch.py`.
- Raw rent levels are deliberately not added to the demand map; the derived LHA-vs-market-rent spread is separate future work.
