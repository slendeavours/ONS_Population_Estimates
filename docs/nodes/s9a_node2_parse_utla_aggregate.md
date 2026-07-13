# S9a Node 2: Parse UTLA Aggregate Rows

- **Type:** Python (openpyxl)
- **Purpose:** Extract UTLA Aggregate rows from each DRD monthly file's "UTLA Acceptable" sheet, coercing suppressed values to NULL.
- **Credential:** None.

## Logic

1. Open each `.xlsx` file in read-only mode with `data_only=True`.
2. Select the sheet matching `*UTLA*Acceptable*` (case-insensitive).
3. Header row is at row 15 (0-indexed row 14). Data starts at row 16.
4. Filter rows where column 0 (`Summary Type`) = `"UTLA Aggregate"`.
5. Exclude rows with NULL, empty, or non-E-prefixed UTLA codes (column 1).
6. Extract 15 metric columns per row (see column mapping below).
7. Coerce suppression markers (`-`, `*`, `..`, blanks) to NULL — never to zero.

## Column Mapping (0-indexed)

| Column | Field | Type |
|---|---|---|
| 1 | utla_code | VARCHAR |
| 2 | utla_name | VARCHAR |
| 8 | pct_acceptable_trust_coverage | NUMERIC (ratio 0–1) |
| 9 | total_discharges | INTEGER (always NULL for UTLA Aggregate — per-provider field) |
| 10 | total_discharges_acceptable_trusts | INTEGER |
| 11 | total_bed_days_lost | INTEGER |
| 13 | pct_same_day_discharge | NUMERIC (ratio 0–1) |
| 14 | pct_delayed_1plus_days | NUMERIC (ratio 0–1) |
| 16–22 | discharged_no_delay through discharged_21_plus_days | INTEGER |
| 46 | avg_days_drd_to_discharge_inc_zero | NUMERIC |
| 47 | avg_days_drd_to_discharge_exc_zero | NUMERIC |

## Suppression Conventions

The DRD files use `-` to indicate not applicable (e.g. `total_discharges` at aggregate level) and `*` for statistical suppression. Both are coerced to NULL.

## Verified Output

- 153 UTLA Aggregate rows per monthly file across all 26 files.
- Verified 2024-07-13.
