# Node 2 - Process and filter to scope

## Type
Python transform (`scripts/s11_cqc_process.py`)

## Purpose
Filter the converted register to the confirmed scope (Adult social care directorate rows) and normalise the file's Y/blank flag columns to booleans, ready for LA mapping. Both model lenses filter this one table: HSS reads `supported_living` and `personal_care` directly; UCWS reads the whole table as provider-landscape context.

## Code
`scripts/s11_cqc_process.py` (full content in the repo). Column mappings are the `TEXT_COLS` and `FLAG_COLS` dicts at the top of the script.

## Logic
1. Read `HSCA_Active_Locations.csv` as strings, blanks preserved (`keep_default_na=False`).
2. Keep rows where `Location Inspection Directorate` = `Adult social care`. July 2026 file: 30,497 of 56,870 rows. Dentists, GPs and hospitals fall away; care homes, domiciliary care, extra care, Shared Lives and supported living remain so either lens can query without a rebuild.
3. Dormant and dual-registered rows are kept and flagged, never dropped. The dual `Primary ID` is carried through so provision counts can dedupe the 852 dual-registered rows per the guidance in CQC's own README sheet.
4. Normalise sixteen Y/blank columns to booleans (service types, regulated activities, service user bands, dormant). `Inherited Rating (Y/N)` is Y/N/blank and keeps blank as null. `Brand Name` uses `-` as its blank marker; that becomes null too.
5. Parse latitude/longitude and care home beds as numerics, registration and rating publication dates as dates.
6. Assert location IDs are present and unique, then write `data/processed/cqc_locations_processed.csv`.

## Behaviour
Re-run safe: pure file transform, overwrites its output. Asserts fail loudly on duplicate or missing location IDs.

## Connection
- Input: Node 1 - Fetch CQC care directory with filters
- Output: Node 3 - Resolve LAD24CD

## Verified Output (2026-07-12)
30,497 rows in scope. Flag totals: supported_living 4,930; personal_care 15,884; care_home 14,877; domiciliary_care 14,381; extra_care_housing 736; shared_lives 162; dormant 1,005; dual_registered 852. Nine rows without coordinates. Totals match the Phase 1 profile of the raw file exactly.
