# Node 1 - Fetch DWP LHA CSV

> **Superseded 2026-10-11.** This note describes the retired S14 build: `scripts/s14_lha_rates_build.py`, `scripts/s14_lha_rates_build_v2.py` and the n8n workflow "LHA Rates + List of Rents (S14)". Those scripts ran by accident on 2026-08-20 and 2026-10-11 and each time left authorities with no rate row in W1's join; they are now guards that exit 2 and must not be run. S14 is loaded by `scripts/s14_lha_editions.py`, which finds the newest England file in the GOV.UK collection, reads the year from the CSV header, halts on a blank, text or negative rate, skips no row, derives the weekly rates with exact decimals and keeps every file's statement as an edition (`brma_lha_rates_editions`, `la_brma_mapping_editions`). The source note is `docs/s14_lha_source.md`. The text below is left as written and describes the old method.

## Type
HTTP GET + CSV Parse

## Purpose
Fetch the official DWP Universal Credit LHA rates for all 152 English BRMAs (FY 2026-27). Rates are frozen at April 2024 levels, published as monthly values per bedroom category.

## URL
```
https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv
```

## Logic
1. HTTP GET from DWP assets bucket
2. Parse CSV: row 0 is the single header row (BRMA, SAR monthly, 1 Bed, 2 Bed, 3 Bed, 4 Bed), data starts row 1. **NOTE: the file has no separate title row** — treating row 1 as headers silently drops the first BRMA (Ashford)
3. For each BRMA row:
   - Extract BRMA name (column 0)
   - Extract monthly rates (columns 1-5): SAR, 1-bed, 2-bed, 3-bed, 4-bed
   - Remove GBP symbol and comma thousands separators
   - Convert to float
4. Transform monthly -> weekly: `weekly = monthly * 12 / 52`, round to 2 decimals
5. Output: List of 152 BRMA dicts with monthly and weekly rates

## Behaviour
- Deterministic: CSV is source-of-truth, safe to re-run
- Parsed data is held in memory (not persisted until Task 4)
- 152 BRMAs parsed (England only, excl. Scotland/Wales/NI)

## Output
Python list of dicts (152 items):
```python
{
  'brma_name': 'Birmingham',
  'sar_monthly': 341.58,
  'sar_weekly': 78.83,
  'one_bed_monthly': 750.00,
  'one_bed_weekly': 173.08,
  ...
}
```

## Verified Output
- 152 BRMAs parsed successfully (2026-07-11; initial run missed Ashford due to header-row miscount, corrected same day)
- Birmingham SAR: GBP78.83/week (verified correct)
- Ashford SAR: GBP90.75/week (monthly GBP393.24)
- All rates in range GBP50-250/week
