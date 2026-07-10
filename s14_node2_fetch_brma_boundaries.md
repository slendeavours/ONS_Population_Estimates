# Node 2 - Fetch BRMA Boundaries (GML)

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
