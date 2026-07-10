# Node 5 - Upsert LA <-> BRMA Mapping

## Type
Postgres UPSERT

## Purpose
Load 296 LA <-> BRMA mapping records into la_brma_mapping table. Safe to re-run.

## Query

```sql
INSERT INTO la_brma_mapping (lad24cd, la_name, brma_name, brma_secondary, mapping_method, source)
VALUES
  ('E08000025', 'Birmingham', 'Birmingham', NULL, 'centroid_spatial_join', 'VOA BRMA May 2020 x la_boundaries centroids'),
  ('E08000012', 'Liverpool', 'Greater Liverpool', NULL, 'centroid_spatial_join', 'VOA BRMA May 2020 x la_boundaries centroids'),
  ('E06000043', 'Brighton and Hove', 'Brighton and Hove', NULL, 'centroid_spatial_join', 'VOA BRMA May 2020 x la_boundaries centroids'),
  ...
  (296 rows total)
ON CONFLICT (lad24cd) DO UPDATE SET
    la_name = EXCLUDED.la_name,
    brma_name = EXCLUDED.brma_name,
    mapping_method = EXCLUDED.mapping_method,
    loaded_at = NOW();
```

## Logic
- INSERT 296 rows (one per LA)
- ON CONFLICT (lad24cd): if LA already exists, UPDATE all columns
- UPDATE all fields except lad24cd (the key)
- Set loaded_at to NOW() on update

## Behaviour
- **Idempotent**: safe to re-run, no duplicate errors
- **Atomic**: all 296 rows inserted/updated together
- **Fast**: batch insert in single statement
- Fallback mappings preserved (mapping_method column distinguishes)

## Parameters
| Parameter | Value |
|-----------|-------|
| Rows inserted/updated | 296 |
| Table | la_brma_mapping |
| Conflict key | lad24cd |
| Financial year | 2026-27 (implicit in mapping source) |

## Connection
- Input: Output of Node 3 (mapping DataFrame)
- Output: la_brma_mapping table populated (296 rows)
