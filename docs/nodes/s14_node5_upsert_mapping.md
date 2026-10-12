# Node 5 - Upsert LA <-> BRMA Mapping

> **Superseded 2026-10-11.** This note describes the retired S14 build: `scripts/s14_lha_rates_build.py`, `scripts/s14_lha_rates_build_v2.py` and the n8n workflow "LHA Rates + List of Rents (S14)". Those scripts ran by accident on 2026-08-20 and 2026-10-11 and each time left authorities with no rate row in W1's join; they are now guards that exit 2 and must not be run. S14 is loaded by `scripts/s14_lha_editions.py`, which finds the newest England file in the GOV.UK collection, reads the year from the CSV header, halts on a blank, text or negative rate, skips no row, derives the weekly rates with exact decimals and keeps every file's statement as an edition (`brma_lha_rates_editions`, `la_brma_mapping_editions`). The source note is `docs/s14_lha_source.md`. The text below is left as written and describes the old method.

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
