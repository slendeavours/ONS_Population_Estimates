# Changelog

All notable changes to this project are documented here.
Format follows Keep a Changelog. Versioning follows semver where tags are used.

## [Unreleased]

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
