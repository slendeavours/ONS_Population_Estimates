# Node 6 — Upsert Table 615 and Seed Series Breaks

## Type

Postgres — Execute Query, batched. `scripts/s22_ctb_empties_build.py`, function `load_all`.

## Purpose

Load the 7,170-row vacant dwellings series and seed the two documented structural breaks.

## Credential

Postgres `exempt_pipeline`.

## Query / Code / URL (full content)

```sql
INSERT INTO la_vacant_dwellings_615 (
    published_la_code, published_la_name, year, vacant_dwellings,
    long_term_vacant_dwellings, lad24cd, mapping_status, loaded_at)
VALUES (%(published_la_code)s, %(published_la_name)s, %(year)s,
        %(vacant_dwellings)s, %(long_term_vacant_dwellings)s,
        %(lad24cd)s, %(mapping_status)s, now())
ON CONFLICT (published_la_code, year) DO UPDATE SET
    published_la_name          = EXCLUDED.published_la_name,
    vacant_dwellings           = EXCLUDED.vacant_dwellings,
    long_term_vacant_dwellings = EXCLUDED.long_term_vacant_dwellings,
    lad24cd                    = EXCLUDED.lad24cd,
    mapping_status             = EXCLUDED.mapping_status,
    loaded_at                  = now();
```

```sql
INSERT INTO ctb_series_breaks (
    first_period, last_period, affected_column, dimension,
    description, comparability, source_url)
SELECT %(first_period)s, %(last_period)s, %(affected_column)s,
       %(dimension)s, %(description)s, %(comparability)s, %(url)s
 WHERE NOT EXISTS (
    SELECT 1 FROM ctb_series_breaks
     WHERE first_period = %(first_period)s
       AND affected_column = %(affected_column)s);
```

Seed content:

| `first_period` | `affected_column` | `dimension` | `description` | `comparability` |
|---|---|---|---|---|
| 2024-04-01 | `empty_homes_premium_count` | premium threshold | From 1 April 2024 authorities could charge an Empty Homes Premium of up to 100% on properties empty for between 1 and 2 years. Previously the premium could only be applied where a property had been empty for 2 or more years. | Not comparable before and after 1 April 2024. The eligible population widened; a rise across this date is a threshold change, not more empty homes. England premium counts rose 27.9% between the 2024 and 2025 taxbase years. |
| 2025-04-01 | `second_homes` | premium introduction | From 1 April 2025 authorities could charge a Second Homes Premium of up to 100% on properties reported as second homes for council tax purposes. In the 2025 taxbase year 211 of 296 authorities applied it. | Affected by reclassification behaviour from 1 April 2025. Authorities reported reviewing empty properties and second homes ahead of the new premium, which moves dwellings between the empty and second home categories independently of any change on the ground. |

`source_url` on both rows is the MHCLG technical notes URL resolved in Node 1.

## Query Parameters

| Parameter | Source |
|---|---|
| `published_la_code` | `ONS code` column, exactly as printed |
| `published_la_name` | `Area` column, exactly as printed |
| `year` | parsed from the snapshot-date column header |
| `vacant_dwellings` | `All_vacants` sheet |
| `long_term_vacant_dwellings` | `All_long_term_vacants` sheet |
| `lad24cd` | resolved in Node 3; null for abolished districts |
| `mapping_status` | `direct`, `resolved_via_lookup` or `unmapped` |
| `url` | technical notes URL from Node 1 |

## Behaviour

Upsert on `(published_la_code, year)`. Historic revisions to Table 615 are corrected in place on the next run.

Barnsley and Sheffield each hold rows under two published codes across the series — the pre-2025 code for the historic years and the post-recode code for 2025. Both are kept as published and both resolve to the same `lad24cd`, so nothing is lost and nothing is double counted at a single year.

The series breaks insert is guarded by `WHERE NOT EXISTS` on `(first_period, affected_column)` rather than by a unique constraint, so re-running neither duplicates the breaks nor overwrites any manual edit made to the description.

## Connection

- Input: Node 5 (Upsert Council Taxbase and Exemption Classes)
- Output: Node 7 (Create Rates View)

## Verified Output

2026-08-13. 7,170 rows in `la_vacant_dwellings_615`, years 2004 to 2025: 6,277 `direct`, 891 `unmapped` across 80 abolished district codes, 2 `resolved_via_lookup`. 2 rows in `ctb_series_breaks`. Second full load left both unchanged.
