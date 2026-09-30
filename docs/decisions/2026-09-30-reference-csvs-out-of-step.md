# Five reference CSVs disagreed with their originals, 2026-09-30

## What happened

A sense check of `data/reference/` against national benchmarks flagged large
variances. Tracing each to the original returns showed the causes were in the
CSV copies, not the data. The database and the map were already correct.

| File | Defect | Consequence |
|---|---|---|
| `rough_sleeping_snapshot_2025.csv` | `rough_sleeping_2025` and `_2024` held the 2021 and 2020 snapshots | Sum 2,443 against a published 4,793 |
| `statutory_homelessness_2025Q2.csv` | mental health, learning disability, drug, alcohol and rough sleeping history read four columns out of Table A3 | Same defect `s1_quarantine_support_need_columns.py` already quarantined in the database |
| `statutory_homelessness_2025Q3.csv` | drug, alcohol and rough sleeping history read wrongly | As above |
| both statutory files | suppressed `..` cells written as 0 | Twelve LAs looked like zero TA |
| `ro4_housing_expenditure_2024-25.csv` | held the **second** release (4 Dec 2025, 9 LAs missing); `hra_admin_prevention_relief_net_exp_000` held TA administration net | Nine LAs blank that the third release (11 Jun 2026) supplies; one column wrong |
| `lahs_waiting_list_2015_2025.csv` | three 2025 values differed (Bromley, Hillingdon, Rushcliffe) | Total 92 out |

All were corrected in place from the originals. `scripts/verify/verify_reference_csvs.py`
reruns the comparison and passes 37 of 37.

## Why nothing caught it

The CSVs are provenance copies. Nothing reads them at runtime, so a wrong copy
raises no error and the pipeline stays correct. It only surfaces when someone
uses the CSV directly. The `source` label on the RO4 CSV still says "published
18 Sep 2025" (the first release) although the values are now the third; the
database carries the same label. Not changed, to keep parity with the database.

## Still open (database, not changed)

1. **`ro4_housing_expenditure.hra_admin_prevention_relief_net_exp_000` holds
   Temporary accommodation administration net expenditure**, not the Homelessness
   Reduction Act line (258 of 296 rows differ). Nothing in `la_signals.sql` reads
   it, so the map is unaffected. Same shape as the support-need defect: a plausible
   number under the wrong name.
2. **`la_housing_register` keeps one arbitrary predecessor row where a
   reorganised authority has several.** LAHS reports the old authorities
   separately, and the loader maps them all to the successor code. The database
   has a unique key on `(lad24cd, reporting_year)`, so only one survives; for BCP
   2015 it kept 946 (the third row) and for 2016 it kept 4,481 (the first). The
   correct successor history is the sum of the predecessors. Affects 11
   authorities (BCP, Dorset, Buckinghamshire, North and West Northamptonshire,
   Cumberland, Westmorland and Furness, North Yorkshire, Somerset, East and West
   Suffolk), 2015 to 2023. The map uses 2025 only, so it is unaffected. The CSV
   carries 215 duplicate rows for the same reason.
3. `total_housing_gross_exp_000` and `_net` differ from the third release in
   10 database rows.

## Lessons

- A check against a national total is only as good as the total. The brief's
  £2.93bn and 135,580 were both mislabelled or untraceable; the second was a
  real figure for a different quarter. Trace a benchmark to its table before
  treating a variance as a defect.
- Check which release a source file is. Three releases of the same RO4 workbook
  differ in how many authorities they cover, and the file name does not say.

## Addendum, same day

Assessed after the record was written. The map reads `la_housing_register` at
the latest year only, and reads none of `hra_admin_prevention_relief_net_exp_000`,
`total_housing_gross_exp_000` or `total_housing_net_exp_000`, so all three
database defects are latent, not live. Item 3 above is an incomplete refresh:
eight authorities have a homelessness total but a NULL housing total, and
Islington (-£320k) and Leicester (+£2.2m) differ from the third release. The
database's statutory homelessness tables were compared with the corrected CSVs
for both quarters and agree on every checked column.

