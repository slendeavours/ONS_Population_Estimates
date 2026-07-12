# Node 5 - Upsert locations

## Type
Postgres parameterised upsert, one operation (`scripts/s11_cqc_load.py`, node 5 step)

## Purpose
Load the mapped rows into `cqc_locations`, keyed on `location_id`. Existing rows are updated in place; a location that reappears after a deregistration is reactivated, because the register file is the source of truth for active status.

## Query
```sql
INSERT INTO cqc_locations (location_id, provider_id, provider_name, brand_name,
    location_name, postcode, latitude, longitude, lad24cd, region, la_name_cqc,
    mapping_method, supported_living, personal_care, care_home, care_homes_beds,
    domiciliary_care, extra_care_housing, shared_lives,
    accommodation_nursing_personal_care, band_learning_disabilities_autism,
    band_mental_health, band_younger_adults, band_older_people, band_dementia,
    band_substance_misuse, band_physical_disability, band_detained_mha, dormant,
    dual_registered, dual_primary_id, latest_overall_rating,
    rating_publication_date, inherited_rating, inspection_directorate,
    primary_inspection_category, location_hsca_start_date, source_file_date)
VALUES %s
ON CONFLICT (location_id) DO UPDATE SET
    <every non-key column> = EXCLUDED.<column>,
    is_active = TRUE,
    deregistered_seen_date = NULL,
    loaded_at = NOW()
```
Executed with `psycopg2.extras.execute_values`, page size 2,000, values fully parameterised. The full SET list is generated from the column list in the script (`UPSERT_COLS`), so the doc and the code cannot drift on column names.

## Query Parameters
| Parameter | Source |
|---|---|
| 38 columns per row | `data/processed/cqc_locations_mapped.csv`, one tuple per location |
| `source_file_date` | `data/raw/s11_csv/FILE_DATE.txt`, written by Node 1 |

## Behaviour
Conflict on the primary key updates every data column and resets the lifecycle fields (`is_active = TRUE`, `deregistered_seen_date = NULL`). Re-running the same file produces identical row counts and no duplicates.

## Connection
- Input: Node 4 - Create cqc_locations
- Output: Node 6 - Deactivation sweep

## Verified Output (2026-07-12)
30,492 rows upserted from the 2026-07-01 file; second run same counts, no duplication.
