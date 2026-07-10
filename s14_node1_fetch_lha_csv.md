# Node 1 - Fetch DWP LHA CSV

## Type
HTTP GET + CSV Parse

## Purpose
Fetch the official DWP Universal Credit LHA rates for all 151 English BRMAs (FY 2026-27). Rates are frozen at April 2024 levels, published as monthly values per bedroom category.

## URL
```
https://assets.publishing.service.gov.uk/media/69d654a2e1430e837a86f64a/england-rates-2026-to-2027.csv
```

## Logic
1. HTTP GET from DWP assets bucket
2. Parse CSV: skip row 0 (title), use row 1 as column headers (BRMA, SAR monthly, 1 Bed, 2 Bed, 3 Bed, 4 Bed), data starts row 2
3. For each BRMA row:
   - Extract BRMA name (column 0)
   - Extract monthly rates (columns 1-5): SAR, 1-bed, 2-bed, 3-bed, 4-bed
   - Remove GBP symbol and comma thousands separators
   - Convert to float
4. Transform monthly -> weekly: `weekly = monthly * 12 / 52`, round to 2 decimals
5. Output: List of 151 BRMA dicts with monthly and weekly rates

## Behaviour
- Deterministic: CSV is source-of-truth, safe to re-run
- Parsed data is held in memory (not persisted until Task 4)
- 151 BRMAs parsed (England only, excl. Scotland/Wales/NI)

## Output
Python list of dicts (151 items):
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
- 151 BRMAs parsed successfully (2026-07-10)
- Birmingham SAR: GBP78.83/week (verified correct)
- All rates in range GBP50-250/week
