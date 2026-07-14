# Node 4 — Process and Load

## Type
Python script task

## Purpose
Filter, recode, merge and upsert HPI data into `la_house_prices`.

## Logic
1. Load File 1 (all-property prices) and File 2 (by property type)
2. Build File 2 lookup keyed by (Area_Code, Date)
3. Filter File 1 to English LAs only (E06, E07, E08, E09 prefixes)
4. Filter to periods >= 2022-01-01
5. Apply hard recodes: E08000038 → E08000016 (Barnsley), E08000039 → E08000019 (Sheffield)
6. Reconcile remaining codes via `la_code_lookup` (old_code → new_code) and `la_boundaries`
7. Merge File 2 on (Area_Code, Date) to add property-type price columns
8. Store empty/NaN values as NULL
9. Upsert into `la_house_prices` in batches of 500 using ON CONFLICT DO UPDATE

## Key parameters
| Parameter | Value |
|---|---|
| Target table | `la_house_prices` |
| Geography filter | E06, E07, E08, E09 |
| Period filter | >= 2022-01-01 |
| Hard recodes | E08000038 → E08000016, E08000039 → E08000019 |
| Batch size | 500 rows |
| Upsert key | (lad24cd, period) |

## Behaviour
Idempotent — upsert overwrites existing rows for the same (lad24cd, period). Safe to re-run on updated editions. Suppressed values stored as NULL, never estimated.

## Verified output
15,340 rows upserted across 295 LAs and 52 periods (2022-01 to 2026-04). Zero unresolved codes. Zero File 2 unmatched rows. 2026-07-14.
