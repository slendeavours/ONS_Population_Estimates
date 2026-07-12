# Changelog

All notable changes to this project are documented here.
Format follows Keep a Changelog. Versioning follows semver where tags are used.

## [Unreleased]

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
