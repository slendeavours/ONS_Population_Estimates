# Node 2 - Fetch BRMA Boundaries (GML)

> **Superseded 2026-10-11.** This note describes the retired S14 build: `scripts/s14_lha_rates_build.py`, `scripts/s14_lha_rates_build_v2.py` and the n8n workflow "LHA Rates + List of Rents (S14)". Those scripts ran by accident on 2026-08-20 and 2026-10-11 and each time left authorities with no rate row in W1's join; they are now guards that exit 2 and must not be run. S14 is loaded by `scripts/s14_lha_editions.py`, which finds the newest England file in the GOV.UK collection, reads the year from the CSV header, halts on a blank, text or negative rate, skips no row, derives the weekly rates with exact decimals and keeps every file's statement as an edition (`brma_lha_rates_editions`, `la_brma_mapping_editions`). The source note is `docs/s14_lha_source.md`. The text below is left as written and describes the old method.

## Type
HTTP GET + Geospatial Download + Reproject

## Purpose
Fetch the VOA Broad Rental Market Area (BRMA) boundary layer from GOV.UK. Used as the spatial reference for mapping 296 LA centroids to their corresponding BRMAs.

## URL
Publication page: `https://www.gov.uk/government/publications/broad-rental-market-area-boundary-layer-for-geographical-information-system-gis-applicable-may-2020`
(Extract shapefile/GML ZIP download URL from page)

## Logic
1. HTTP GET publication page
2. Parse HTML for `href` attribute containing `.zip`
3. Download ZIP archive (format: GML or Shapefile)
4. Extract to temporary directory
5. Load with geopandas:
   - Try `.gml` files first (VOA now publishes in GML format)
   - Fall back to `.shp` files if no GML
6. Check CRS:
   - If None: set to EPSG:27700 (British National Grid, VOA default)
   - If not EPSG:4326: reproject to WGS84
7. Identify BRMA name column (usually 'Name' or 'BRMA')
8. Output: GeoDataFrame with geometry (polygons) + 'brma_name' column

## Behaviour
- Safe to re-run; remote file is source of truth
- Temporary files cleaned up after loading
- CRS handling is automatic
- Supports both GML and Shapefile formats

## Output
Geopandas GeoDataFrame (152 rows, geometry + attributes):
```
    Name (BRMA name)  geometry (Polygon, EPSG:4326)
0   Aylesbury         POLYGON((-0.65 51.8, ...))
1   Barnsley          POLYGON((-1.5 53.5, ...))
...
152 York              POLYGON((-1.1 54.0, ...))
```

## Verified Output
- 152 BRMA geometries loaded (2026-07-10)
- CRS: EPSG:4326 (WGS84 for spatial join with LA centroids)
- Name column identified and used for joining
