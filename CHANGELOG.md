# Changelog

All notable changes to this project are documented here.
Format follows Keep a Changelog. Versioning follows semver where tags are used.

## [Unreleased]

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
