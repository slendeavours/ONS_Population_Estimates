# S4 Node 5 — Create care_leaver_accommodation Table

**Type:** Postgres — Execute Query

## Purpose

Creates `care_leaver_accommodation` if absent. Primary key `(lad24cd, reporting_year, age_group)`: one row per authority per year per cohort. The 17-21 cohort carries accommodation bucket counts; the 22-25 cohort carries suitability counts only.

## Schema

```sql
CREATE TABLE IF NOT EXISTS care_leaver_accommodation (
    lad24cd                     VARCHAR(9)  NOT NULL,
    reporting_year              INTEGER     NOT NULL,
    age_group                   VARCHAR(5)  NOT NULL,
    total_care_leavers          INTEGER,
    semi_independent            INTEGER,
    independent_living          INTEGER,
    with_family                 INTEGER,
    community_home              INTEGER,
    unsuitable                  INTEGER,
    other                       INTEGER,
    not_known                   INTEGER,
    suitable_count              INTEGER,
    unsuitable_pct              NUMERIC(5,2),
    semi_independent_published  INTEGER,
    total_published             INTEGER,
    suppressed_flag             BOOLEAN     DEFAULT FALSE,
    uasc_impact_flag            BOOLEAN     DEFAULT FALSE,
    source                      TEXT,
    loaded_at                   TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (lad24cd, reporting_year, age_group)
);
```

## Column notes

| Column | Notes |
|---|---|
| `lad24cd` | LAD24CD for unitary and metropolitan authorities. County councils are carried on their own `E10` code and will not join `la_boundaries` |
| `reporting_year` | Year ending 31 March, stored as integer |
| `age_group` | `17-21` or `22-25` |
| `semi_independent` | Pipeline aggregate: semi-independent transitional + Foyers + Supported lodgings. From 2024, the underlying DfE category means Ofsted-registered provision only |
| `semi_independent_published` | DfE's published `Semi-independent, transitional accommodation` alone. **Use this for anything external** |
| `total_care_leavers` | Sum of buckets, so a minimum where suppression is present |
| `total_published` | DfE's own Total row. The correct total to quote |
| `suppressed_flag` | True where any cell contributing to `semi_independent` was suppressed |
| `unsuitable` | 17-21: B&B + emergency + no fixed abode. 22-25: accommodation deemed unsuitable |
| `suitable_count` | 22-25 only |
| `unsuitable_pct` | 22-25 only |
| `uasc_impact_flag` | Reserved, not populated |

Columns `semi_independent_published`, `total_published` and `suppressed_flag` were added 2026-08-20.

## Connection

- Input: Process CLA Data (Node 4)
- Output: Upsert Data (Node 6)

## Verified Output

Table created successfully. (2026-03-31)
