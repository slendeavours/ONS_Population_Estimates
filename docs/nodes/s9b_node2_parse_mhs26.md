# S9b Node 2: Parse MHS26 at LA Level

- **Type:** Python (csv module)
- **Purpose:** Extract MHS26 rows at Local Authority level from MHSDS monthly data CSVs, filtering to E06/E07/E08/E09 codes only.
- **Credential:** None.

## Logic

1. Read each CSV with `csv.DictReader`.
2. Filter rows where:
   - `MEASURE_ID` = `MHS26`
   - `BREAKDOWN` contains `local authority` (case-insensitive)
   - `PRIMARY_LEVEL` starts with E06, E07, E08, or E09
   - `SECONDARY_LEVEL` = `NONE`
3. Extract `MEASURE_VALUE` as integer. Coerce `*` (statistical suppression) to NULL.
4. E10 county codes and `UNKNOWN` rows are excluded per Gate 2 approval.

## MHS26 Measure

- **Full name:** Clinically Ready for Discharge (CRFD)
- **Unit:** Delayed discharge bed days
- **Cohort:** Combined mental health + learning disability + autism (no disaggregation available at source)
- **Source:** MHSDS (Mental Health Services Data Set)
- **NOT in timeseries files** — only available in individual monthly data files

## Suppression

- `*` = small number suppression (coerced to NULL)
- Suppression rate: 28–46% of LAs per month (82–136 out of 296)

## Barnsley/Sheffield Code Transition

From June 2025, MHSDS uses E08000038 (Barnsley) and E08000039 (Sheffield) instead of E08000016/E08000019. Both code variants are loaded as-is; resolution handled by `vw_mh_crfd_lad` view via `la_code_lookup`.

## Verified Output

- 296 LA rows per monthly file across all 38 files.
- Verified 2024-07-13.
