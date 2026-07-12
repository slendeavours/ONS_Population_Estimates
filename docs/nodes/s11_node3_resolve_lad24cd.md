# Node 3 - Resolve LAD24CD by spatial join

## Type
Spatial join + tiered fallbacks (`scripts/s11_cqc_map.py`)

## Purpose
Assign every location a May 2024 district code (`lad24cd`) that exists in `la_boundaries`. The file's own `Location Local Authority` column cannot do this: it holds 155 upper-tier names (Suffolk, Kent, Hampshire) rather than the 296 LAD24 districts, so the spatial route is the primary method and the name is stored for audit only. This is the S14 pattern inverted: S14 dropped LA centroids into BRMA polygons; S11 drops location points into LA polygons.

## Code
`scripts/s11_cqc_map.py` (full content in the repo).

## Logic
1. Load `la_boundaries` polygons (geojson jsonb column, 296 rows) into geopandas as EPSG:4326, and the historical-code map from `la_code_lookup`.
2. Point-in-polygon join (`sjoin`, predicate `within`) of location lat/long points into the district polygons. Points landing exactly on a shared boundary can match twice; the first match is kept. `mapping_method = 'point_in_polygon'`.
3. Points outside every polygon (coastal or cross-border coordinate error): both layers reproject to EPSG:27700 and the nearest polygon is assigned, distance printed in metres. `mapping_method = 'nearest_fallback'`.
4. Rows with no usable coordinates: postcode resolved via `api.postcodes.io` (bulk endpoint, ONS-backed). A returned code absent from `la_boundaries` is reconciled through `la_code_lookup` before use, and only stored if the final code exists in `la_boundaries`. `mapping_method = 'postcode_api_fallback'`.
5. Postcodes the live endpoint does not know: the `terminated_postcodes` endpoint still returns coordinates, which then go through the normal point-in-polygon assignment. `mapping_method = 'postcode_terminated_fallback'`.
6. Rows that still cannot be mapped are excluded from the load and listed in full. The pipeline never stores a null `lad24cd`.
7. Cross-check (never the join): where the CQC LA name exactly equals a `lad24nm` (unitaries and metropolitan districts), compare it with the spatial assignment and report the agreement rate.

## Behaviour
Re-run safe: pure transform over its input CSV plus read-only database queries. Asserts that every mapped code exists in `la_boundaries`.

## Connection
- Input: Node 2 - Process and filter to scope
- Output: Node 4 - Create cqc_locations

## Verified Output (2026-07-12)
30,492 of 30,497 rows mapped: point_in_polygon 30,486; postcode_api_fallback 3; nearest_fallback 2 (both Cheshire West and Chester, 379 m and 669 m outside the polygon); postcode_terminated_fallback 1 (ST16 1JJ, terminated October 2023, resolved to Stafford). Five rows excluded, postcodes unknown to postcodes.io (new postcodes ahead of the ONSPD edition, or CQC typos); they re-resolve automatically on a later monthly run once ONSPD catches up. Name cross-check: 19,517 of 19,519 comparable rows agree (99.99%); both disagreements are boundary-adjacent postcodes where the ONSPD-derived point sits just across a borough line from the CQC name.
