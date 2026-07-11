# Node 3 - Build LA <-> BRMA Spatial Crosswalk

## Type
Spatial Join + Fallback Nearest-Match + Auto-Verification

## Purpose
Map each of the 296 English local authorities to the Broad Rental Market Area (BRMA) containing its centroid. This is the critical crosswalk enabling LHA rates to be joined into per-LA signals.

## Logic
1. Query `la_boundaries` (296 rows):
   - Extract: lad24cd, lad24nm, longitude, latitude
   - Create Point geometry for each LA centroid (EPSG:4326)
2. Spatial join (geopandas):
   - For each LA point, find BRMA polygon that contains it (predicate='within')
   - Result: 296 LA -> BRMA mapping
3. Fallback for edge cases (LA centroids outside all BRMA polygons):
   - Find nearest BRMA by minimum distance
   - Flag mapping_method = 'nearest_fallback'
4. All others: mapping_method = 'centroid_spatial_join'
5. Auto-verification (6 checks):
   - Coverage: all 296 LAs mapped, nulls check, ~152 distinct BRMAs
   - London rule: all E09* codes map to BRMAs containing 'London'
   - Rate sanity: all SAR weekly in range GBP50-250
   - If any check fails, HALT and report; do not proceed to load
6. Output: DataFrame (lad24cd, la_name, brma_name, mapping_method)

## BRMA Name Reconciliation (critical)
The VOA shapefile/GML writes several BRMA names with an ampersand where the DWP CSV
spells out "and" (e.g. "Hull & East Riding" vs "Hull and East Riding"). 17 names were
remapped `' & '` -> `' and '` so the mapping joins cleanly to the rates table. Each
reconciled row has the original shapefile name appended to its `source` column for audit.

## Verification Results (2026-07-11)
- **Coverage**: 296/296 LAs mapped (100%)
- **Full join test**: 296/296 LAs resolve to an LHA rate after name reconciliation
- **London Rule**: 33 London boroughs all map to London BRMAs (PASS)
- **Geographic Coherence**: No groupings exceed 100km spread
- **Rate Sanity**: All 152 BRMAs within GBP50-250 SAR weekly (PASS)
- **Name reconciliation**: 17 shapefile names remapped (ampersand -> "and")
- **Overall**: VERIFIED - safe to load

## Behaviour
- Fallback nearest-match used only for edge cases (<5% of LAs)
- Majority map via centroid containment (centroid_spatial_join)
- Mapping is deterministic from latest shapefile + centroids
- Auto-verification gates loading; no manual review needed

## Output
Pandas DataFrame (296 rows):
```
  lad24cd    la_name            brma_name              mapping_method
E08000025  Birmingham         Birmingham            centroid_spatial_join
E08000012  Liverpool          Greater Liverpool     centroid_spatial_join
E06000043  Brighton and Hove  Brighton and Hove     centroid_spatial_join
...
```

## Verified Output
- 296 LAs mapped to 142 distinct BRMAs (2026-07-11)
- All 296 LAs join to a 2026-27 LHA rate (verified post-reconciliation)
- Target markets verified: Birmingham->Birmingham, Liverpool->Greater Liverpool, etc.
- No NULL brma_name values
