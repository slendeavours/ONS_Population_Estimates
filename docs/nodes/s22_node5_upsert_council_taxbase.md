# Node 5 — Upsert Council Taxbase and Exemption Classes

## Type

Postgres — Execute Query, batched. `scripts/s22_ctb_empties_build.py`, function `load_all`.

## Purpose

Load the 296 authority records and the 3,256 exemption class records, upserting on the natural key so a re-run corrects rather than duplicates.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

```sql
INSERT INTO la_council_taxbase_empties (
    lad24cd, la_name, taxbase_year, total_dwellings,
    empty_under_6_months, empty_6_months_plus, empty_total,
    empty_homes_premium_count, second_homes,
    unoccupied_exemptions_total, source_publication, loaded_at)
VALUES (%(lad24cd)s, %(la_name)s, %(taxbase_year)s,
        %(total_dwellings)s, %(empty_under_6_months)s,
        %(empty_6_months_plus)s, %(empty_total)s,
        %(empty_homes_premium_count)s, %(second_homes)s,
        %(unoccupied_exemptions_total)s, %(source_publication)s, now())
ON CONFLICT (lad24cd, taxbase_year) DO UPDATE SET
    la_name                     = EXCLUDED.la_name,
    total_dwellings             = EXCLUDED.total_dwellings,
    empty_under_6_months        = EXCLUDED.empty_under_6_months,
    empty_6_months_plus         = EXCLUDED.empty_6_months_plus,
    empty_total                 = EXCLUDED.empty_total,
    empty_homes_premium_count   = EXCLUDED.empty_homes_premium_count,
    second_homes                = EXCLUDED.second_homes,
    unoccupied_exemptions_total = EXCLUDED.unoccupied_exemptions_total,
    source_publication          = EXCLUDED.source_publication,
    loaded_at                   = now();
```

```sql
INSERT INTO la_ctb_exemption_classes (
    lad24cd, taxbase_year, exemption_class, exemption_description,
    dwellings, loaded_at)
VALUES (%(lad24cd)s, %(taxbase_year)s, %(exemption_class)s,
        %(exemption_description)s, %(dwellings)s, now())
ON CONFLICT (lad24cd, taxbase_year, exemption_class) DO UPDATE SET
    exemption_description = EXCLUDED.exemption_description,
    dwellings             = EXCLUDED.dwellings,
    loaded_at             = now();
```

## Query Parameters

`la_council_taxbase_empties`:

| Parameter | Source | Notes |
|---|---|---|
| `lad24cd` | published ONS code, recodes resolved in Node 2 | join key to `la_boundaries` |
| `la_name` | `Local Authority` column | as published |
| `taxbase_year` | release year from Node 1 | 2025 |
| `total_dwellings` | Table 1.01, Total | |
| `empty_under_6_months` | Table 1.18 minus Table 1.19 | derived |
| `empty_6_months_plus` | Table 1.19, Total | long-term empty |
| `empty_total` | Table 1.18, Total | |
| `empty_homes_premium_count` | Table 1.17, Total | zero where the authority applies no premium |
| `second_homes` | Table 1.11, Total | |
| `unoccupied_exemptions_total` | Table 2.01, sum of classes B, D, E, F, G, H, I, J, K, L, Q | |
| `source_publication` | release title, attachment title, first published and revised dates | |

`la_ctb_exemption_classes`:

| Parameter | Source |
|---|---|
| `lad24cd` | as above |
| `taxbase_year` | as above |
| `exemption_class` | class letter from the Table 2.01 header |
| `exemption_description` | fixed description of the class |
| `dwellings` | Table 2.01, that class's column |

## Behaviour

Every statement is parameterised. Nothing is concatenated into SQL.

`ON CONFLICT ... DO UPDATE` on the natural key. Re-running the same release rewrites the same values and moves `loaded_at`; loading a new release year inserts 296 new rows and leaves the previous year untouched, so the series accumulates one release at a time.

Runs inside the load transaction with the DDL and the hard gates. A gate failure rolls all of it back.

## Connection

- Input: Node 4 (Create Tables)
- Output: Node 6 (Upsert Table 615 and Seed Series Breaks)

## Verified Output

2026-08-13. 296 rows in `la_council_taxbase_empties` at `taxbase_year` 2025; 3,256 rows in `la_ctb_exemption_classes` across 296 authorities. Second full load produced identical row counts and value sums.
