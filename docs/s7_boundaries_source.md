# S7 — ONS Local Authority Districts (May 2024) boundaries, generalised clipped (BGC)

<!-- repo-meta
status: active
last-reviewed: 2026-10-11
type: source
consumed-by: scripts/s7_boundaries_editions.py, scripts/s7_boundaries_editions_verify.py
-->

| | |
|---|---|
| Publisher | ONS, through the Open Geography Portal (the item's access information: "Office for National Statistics"; licence page https://www.ons.gov.uk/methodology/geography/licences) |
| Series | "Local Authority Districts (May 2024) Boundaries UK BGC": ArcGIS FeatureServer `Local_Authority_Districts_May_2024_Boundaries_UK_BGC`, layer 0 `LAD_MAY_2024_UK_BGC`. Service list: https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services |
| Cadence | On boundary changes: the publisher issues a new dated vintage rather than revising an old one. The May 2024 layer's `lastEditDate` is 2024-07-02 13:28:58 UTC |
| Live table | `la_boundaries` (the W1 spine) |
| Editions | `la_boundaries_editions` (append-only, no foreign key) and `la_boundaries_editions_file_checks` (ledger) |
| Natural key | `lad24cd` (the live table holds one vintage; `source_date` is the engine period) |
| Held | 296 English districts, one vintage (May 2024, `source_date` 2024-05-01); edition 1 "as loaded" |
| Loader | `scripts/s7_boundaries_editions.py`; gates `scripts/s7_boundaries_editions_verify.py` (19) |
| Record | `docs/decisions/2026-10-10-reference-trio-editions-first-load.md` |

**The W1 spine and the map's geometry.** `sql/w1/05_la_signals.sql` reads `FROM la_boundaries`; `scripts/export_map_data.py` exports `lad24cd` and `geojson` to `data/boundaries/la_boundaries.geojson`; 20 foreign keys point at the table (counted from `pg_constraint` on 2026-10-11). The loader therefore never deletes, re-keys, inserts or rebuilds a live row, and it never writes `la_code_lookup`. The first load changed no held value.

## What is loaded, and what the columns mean

The publisher's item description says: "This file contains the digital vector boundaries for Local Authority Districts, in the United Kingdom, as at May 2024. The boundaries available are: (BGC) Generalised (20m) - clipped to the coastline (Mean High Water mark)." (the item behind the service, read from the ArcGIS sharing API on 2026-10-11). The layer definition declares the fields `FID`, `LAD24CD`, `LAD24NM`, `LAD24NMW`, `BNG_E`, `BNG_N`, `LONG`, `LAT`, `Shape__Area`, `Shape__Length` and `GlobalID`, polygon geometry, and units "esriMeters" for the shape measures; the layer's own description and copyright text are empty. Five attributes are queried and stored, with the geometry:

| Column in `la_boundaries` | Source | Notes |
|---|---|---|
| `lad24cd`, `lad24nm` | `LAD24CD`, `LAD24NM` | English districts only (E06 to E09) |
| `longitude`, `latitude` | `LONG`, `LAT` | The publisher's attributes, `numeric(10,6)`. Described by the publisher only as `LONG` and `LAT`; the loader does not compute them |
| `shape_area` | `Shape__Area` | `numeric(20,2)`. The layer states units of metres for its shape measures. Not recomputed |
| `geojson` | the feature geometry, queried in EPSG:4326 | `jsonb`, Polygon or MultiPolygon (203 Polygon and 93 MultiPolygon) |
| `source_date` | the vintage | 2024-05-01 on all 296 rows |
| `loaded_at` | | 2026-03-25 15:13:11 UTC on all 296 rows (see History) |

**The shapes are the publisher's, unchanged.** All 296 stored geometries are identical to the publisher's layer as queried in EPSG:4326, as canonical JSON text (sorted keys, no spaces) and as `jsonb`, and the five attributes equal it on every district (1,480 cells, 0 differences). They are the generalised "BGC" boundaries "as published", England only. The earlier registry and methodology caveat that they were "simplified to approximately 20% of original vertex count for web performance" had no support: the n8n code did no simplification, and the proof above shows no change. It is withdrawn. (The "20m" in the publisher's description is the BGC generalisation level, the publisher's own, and not a statement about this pipeline.) The publisher's item metadata was modified on 2025-08-11 UTC, after the layer's last edit; metadata is not the data's identity, which is `editingInfo.lastEditDate`.

## Discovery

`load` reads the ArcGIS service list (3,954 services, 15 `Local_Authority_Districts*BGC` FeatureServers on 2026-10-11) and requires exactly one FeatureServer named `Local_Authority_Districts_<vintage>_Boundaries_UK_BGC` for the vintage asked for; none or several halt, listing the names seen. Vintages the service offers and the loader has not declared are named in a NOTE and never loaded (December 2024 is one). It then reads the layer definition (`FeatureServer/0?f=json`) and queries England only (`where LAD<yy>CD LIKE 'E%'`, `outSR=4326`, `f=geojson`), saving both replies to `data/raw/s7_boundaries/` under dated names (git-ignored; a preview downloads too and writes nothing to the database). `--file GEOJSON --layer JSON` takes local copies. A download never overwrites a same-named file with different content.

## Source identity (rule 3), on every path

- **The service** (above) and **the layer definition:** its name (`LAD_MAY_2024_UK_BGC`), type, polygon geometry and the exact field set, with `editingInfo.lastEditDate` (epoch milliseconds) as the **rank**, never a service name, file name or download date. The held layer's is 1719926938757 = 2024-07-02 13:28:58 UTC.
- **The GeoJSON reply:** a FeatureCollection in EPSG:4326 (every coordinate a longitude and latitude in England's range), no `exceededTransferLimit`, exactly the five queried attributes on every feature, Polygon or MultiPolygon geometry, 296 English districts, no code twice. Other nations' codes are read and ignored; any other code halts.
- **The content hash** decides "unchanged" and is what the ledger records, because the GeoJSON bytes are API output.

The held GeoJSON is `data/raw/s7_boundaries/LAD_MAY_2024_UK_BGC_england_4326_2026-10-10.geojson` (bytes sha256 starting 7fd7632b1e00a2c0). A new download on 2026-10-11 was byte-identical.

## Blanks and zeros (rule 1)

Every attribute must be present: a missing or blank name, or a missing, blank or text number, halts (`num_cell`). Nothing is read as 0. The GeoJSON is parsed with exact decimals (the area has more digits than a float keeps) and rounded to the live scales half away from zero, as Postgres numeric does.

## Geography (rule 4)

Declared `old` in `scripts/geography.py`: this source **defines the canonical key**, E08000016 and E08000019 for Barnsley and Sheffield, and is checked against itself (the live key set never changes by a reissue: PARTIAL, no flag releases it). `geography.resolve` is not used; nothing here maps a code.

## Other vintages: previewed, not stored, and not a recode alone

`load --vintage DEC_2025` and `load --vintage May_2026` (declared in the loader's `VINTAGES`) were run as previews on 2026-10-11 and committed nothing; the held files are in `data/raw/s7_boundaries/`. Both print the same difference from the live vintage:

| | May 2024 (live) | December 2025 | May 2026 |
|---|---|---|---|
| Layer | `LAD_MAY_2024_UK_BGC` | `LAD_DEC_2025_UK_BGC` | `LAD_MAY_2026_UK_BGC` |
| `lastEditDate` (UTC) | 2024-07-02 13:28:58 | 2026-04-29 08:34:12 | 2026-09-09 13:34:25 |
| Codes | E08000016, E08000019 | E08000038, E08000039 instead | E08000038, E08000039 instead |
| Features | 93 MultiPolygon, 203 Polygon | 87 and 209 | 87 and 209 |

Codes added 2, removed 2 and 294 shared; **names changed 0**; but **the geometry changed on all 294 shared districts, and `LONG`, `LAT` or `Shape__Area` changed on all 294**. These are redrawn files, not copies of May 2024 with the recode. An earlier survey assumption (that the vintages differ only by the two codes) holds for codes and names only.

**Adopting a newer vintage is Scott's decision.** It re-bases W1's spine and every one of the 20 foreign keys, changes the map's geometry on every district, and flips the canonical Barnsley/Sheffield form (E08000016/19 to E08000038/39, `la_code_lookup`'s recode rows). Nothing in the loader or the plan does it. A later `load --vintage <V> --commit` stores such a vintage as editions only (`held-not-live`); the engine refuses to promote it and `refresh-latest` of a non-live period halts.

## Editions and revisions

An edition is what one ArcGIS query says about its vintage: per English district the name, `LONG`, `LAT`, `Shape__Area` and the geometry. Edition 1 is the table as held (published 2026-03-25), proved against the held reply (above).

A reissue of the live vintage (a later `lastEditDate` with different content) is the next edition. Any change to the live vintage's names or geometry is REJECTED unless `--acknowledge 2024-05-01`, and its `refresh-latest` needs the same flag and prints "this changes the map's geometry: Scott's decision" with the difference. The same `lastEditDate` with different content stops unless `--accept-reissue PERIOD`; a later `lastEditDate` whose only change is in `LONG`, `LAT` or `Shape__Area` is stored as the next edition without a flag and reaches live only through `refresh-latest`. An older layer halts unless `--allow-older-file`. A key-set change is PARTIAL and no flag releases it. An empty `la_boundaries` halts the load: it is never rebuilt here. The registry has `revises_back_series` false: no revision of the May 2024 layer has been seen.

## How to run

Run from `ONS_Population_Estimates`; see the "Reference data" section of `docs/QUARTERLY_REFRESH.md`.

```bash
python scripts/s7_boundaries_editions.py status
python scripts/s7_boundaries_editions.py load                       # preview the live vintage
python scripts/s7_boundaries_editions.py load --vintage DEC_2025    # preview another; nothing reaches live
python scripts/s7_boundaries_editions.py refresh-latest             # preview; --commit needs --acknowledge 2024-05-01
python scripts/s7_boundaries_editions_verify.py
```

Other commands: `ddl` (already run), `restore-edition PERIOD N`, and the one-off `migrate-legacy --file GEOJSON --layer JSON` (already run). `load` takes `--vintage V`, `--file GEOJSON --layer JSON`, `--recheck PERIOD`, `--allow-older-file`, `--accept-reissue PERIOD` and `--acknowledge 2024-05-01`. Every writing command previews unless `--commit` (or `--simulate`, which rolls back).

**Run `refresh-latest` before W1** if it ever applies a change: S7 is the W1 spine and the map's geometry. Gate 17 of the verify script fails by design when `geojson` in live changes: update the `w1-read` line deliberately.

## Known traps

- **Believing the shapes are simplified.** They are the publisher's BGC as published.
- **Believing a newer vintage is a recode.** The Dec 2025 and May 2026 layers are redrawn on every shared district.
- **Rebuilding the table.** 20 foreign keys; rows are never deleted or re-keyed.
- **Writing `la_code_lookup` from this source.** The old workflow did; the loader never does.
- **Trusting the item's modified date.** The layer's `lastEditDate` is the rank.
- **Comparing a geometry as floats.** Compare the canonical text; areas are exact decimals.

## History

- **2026-03-25:** the only run-log row for S7, id 1 (12:38:29 UTC, `source_07_boundaries`, 296 rows), predates the 15:13:11 UTC that every row carries by two and a half hours. The n8n workflow "LA Boundaries (S7)" upserted `la_boundaries` from the same ArcGIS layer (its Code node kept English features and set `source_date` to the literal 2024-05-01; its upsert set `loaded_at = NOW()`), and its "Log Run" node is a `SELECT` on `pipeline_run_log` that writes nothing (read from the workflow itself on 2026-10-11), so a later run or step that was not logged wrote the rows.
- **The workflow's lookup nodes.** After its upsert the workflow also ran `DELETE FROM la_code_lookup WHERE change_type != 'current'` and re-inserted its own March 2026 list, which could have undone the July and August 2026 lookup fixes.
- **2026-10-10:** the workflow is retired (its Code node and its `la_code_lookup` DELETE and INSERT nodes stop with an error naming the new loader).
- **2026-10-11:** `ddl` (editions table and ledger, no foreign key; `la_boundaries` untouched) and `migrate-legacy` (edition 1, run-log 451). A hash over every column of `la_boundaries`, and of `la_code_lookup`, is identical before and after. The record is `docs/decisions/2026-10-10-reference-trio-editions-first-load.md`.
