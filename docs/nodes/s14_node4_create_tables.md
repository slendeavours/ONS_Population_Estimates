# Node 4 - Create Database Tables

> **Superseded 2026-10-11.** This note describes the retired S14 build: `scripts/s14_lha_rates_build.py`, `scripts/s14_lha_rates_build_v2.py` and the n8n workflow "LHA Rates + List of Rents (S14)". Those scripts ran by accident on 2026-08-20 and 2026-10-11 and each time left authorities with no rate row in W1's join; they are now guards that exit 2 and must not be run. S14 is loaded by `scripts/s14_lha_editions.py`, which finds the newest England file in the GOV.UK collection, reads the year from the CSV header, halts on a blank, text or negative rate, skips no row, derives the weekly rates with exact decimals and keeps every file's statement as an edition (`brma_lha_rates_editions`, `la_brma_mapping_editions`). The source note is `docs/s14_lha_source.md`. The text below is left as written and describes the old method.

## Type
Postgres DDL (CREATE TABLE)

## Purpose
Create two tables to persist the BRMA<->LA crosswalk and LHA rates.

## Table 1: la_brma_mapping

```sql
CREATE TABLE la_brma_mapping (
    lad24cd VARCHAR(9) PRIMARY KEY,
    la_name VARCHAR(100),
    brma_name VARCHAR(100) NOT NULL,
    brma_secondary VARCHAR(100),
    mapping_method TEXT DEFAULT 'centroid_spatial_join',
    source TEXT,
    loaded_at TIMESTAMPTZ DEFAULT NOW()
);
```

**Columns:**
- `lad24cd`: ONS Local Authority District code (2024 geography), UNIQUE PK
- `la_name`: Local authority name
- `brma_name`: BRMA the LA is mapped to
- `brma_secondary`: (Reserved for future use - multi-BRMA mappings)
- `mapping_method`: 'centroid_spatial_join' or 'nearest_fallback'
- `source`: Data provenance
- `loaded_at`: Timestamp of load

## Table 2: brma_lha_rates

```sql
CREATE TABLE brma_lha_rates (
    brma_name VARCHAR(100) NOT NULL,
    financial_year VARCHAR(7) NOT NULL,
    sar_weekly NUMERIC(8,2),
    one_bed_weekly NUMERIC(8,2),
    two_bed_weekly NUMERIC(8,2),
    three_bed_weekly NUMERIC(8,2),
    four_bed_weekly NUMERIC(8,2),
    sar_monthly NUMERIC(8,2),
    one_bed_monthly NUMERIC(8,2),
    two_bed_monthly NUMERIC(8,2),
    three_bed_monthly NUMERIC(8,2),
    four_bed_monthly NUMERIC(8,2),
    source TEXT,
    loaded_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (brma_name, financial_year)
);
```

**Columns:**
- `brma_name`: BRMA name (join key to la_brma_mapping)
- `financial_year`: Format 'YYYY-YY' (e.g., '2026-27')
- `sar_weekly`, `*_bed_weekly`: Converted from monthly (monthly * 12 / 52)
- `sar_monthly`, `*_bed_monthly`: Original DWP published values
- Both weekly and monthly stored for audit trail
- `source`: URL to DWP publication
- `loaded_at`: Timestamp of load

## Behaviour
- Both tables use DROP + CREATE (not IF NOT EXISTS) to start fresh
- Primary keys enforce uniqueness:
  - la_brma_mapping: lad24cd (296 rows)
  - brma_lha_rates: (brma_name, financial_year) supports multi-year data
- Timestamps auto-set on creation

## Connection
- Input: None (creates empty tables)
- Output: Empty tables ready for Node 5 & 6 upserts
