# S1 edition diffs, 2026-10-06

Generated from `diff_editions` (edition 2 against edition 1) for the seven quarters 2023Q2 to 2024Q4. Edition 2 is the registry 'revised' file for each quarter, loaded by `scripts/s1_editions.py load` with gates 6-8 run in the same transaction.

## Which files were compared

Each quarter had two candidate files: the older release-page attachment (A) and the registry 'revised' file (B). Both were compared with stored edition 1 in a dry run. The A files are an older vintage and were not loaded as editions. Only B was loaded.

Stored edition 1 turned out to be close to B on the A1 measures: the only A1 differences are zero to NULL for authorities that did not submit (36 authority-quarters, 144 cells; no A1 figure is a numeric revision). The earlier figure of 200 to 230 authorities changed was measured against A, not B; against A the dry run showed:

| Period | Authorities changed, A vs edition 1 | A households_in_ta transitions |
|---|---|---|
| 2023Q2 | 235 of 296 | {'zero -> NULL': 15, 'value -> value': 17, 'value -> NULL': 1} |
| 2023Q3 | 223 of 296 | {'zero -> NULL': 12, 'value -> value': 9} |
| 2023Q4 | 224 of 296 | {'value -> value': 21, 'zero -> NULL': 13} |
| 2024Q1 | 229 of 296 | {'zero -> NULL': 20, 'value -> value': 18} |
| 2024Q2 | 226 of 296 | {'value -> value': 23, 'zero -> NULL': 14} |
| 2024Q3 | 222 of 296 | {'value -> value': 25, 'zero -> NULL': 10} |
| 2024Q4 | 221 of 296 | {'value -> value': 24, 'zero -> NULL': 11} |

Edition 1 support_needs_total matches A (and the S1b table, which was built from A); edition 2 carries the revised A3 figures, which is why support_needs_total accounts for most of the changes below.

## Edition 2 against edition 1

### 2023Q2

Edition 2: `2023Q2_Detailed_LA_202309_revised.ods` (registry, published 2024-04-29), supersedes edition 1.

- authorities changed: 193 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 3 |
| owed_duty | 3 |
| prevention_duty | 3 |
| relief_duty | 3 |
| households_in_ta | 15 |
| support_needs_total | 187 |

- households_in_ta: zero -> NULL 15, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 27, value -> value 186, NULL -> value 1

### 2023Q3

Edition 2: `2023Q3_Detailed_LA_202312_Revised_No_Dropdowns.ods` (registry, published 2024-08-07), supersedes edition 1.

- authorities changed: 184 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 8 |
| owed_duty | 8 |
| prevention_duty | 8 |
| relief_duty | 8 |
| households_in_ta | 12 |
| support_needs_total | 175 |

- households_in_ta: zero -> NULL 12, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 44, value -> value 175

### 2023Q4

Edition 2: `2023Q4_Detailed_LA_202403_nov2024_6747279d.xlsx` (registry, published 2024-11-27), supersedes edition 1.

- authorities changed: 195 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 4 |
| owed_duty | 4 |
| prevention_duty | 4 |
| relief_duty | 4 |
| households_in_ta | 13 |
| support_needs_total | 190 |

- households_in_ta: zero -> NULL 13, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 29, value -> value 190

### 2024Q1

Edition 2: `2024Q1_Statutory_Homelessness_Detailed_Local_Authority_Data_202406_revised.ods` (registry, published 2026-02-24), supersedes edition 1.

- authorities changed: 194 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 7 |
| owed_duty | 7 |
| prevention_duty | 7 |
| relief_duty | 7 |
| households_in_ta | 20 |
| support_needs_total | 183 |

- households_in_ta: zero -> NULL 20, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 48, value -> value 183

### 2024Q2

Edition 2: `2024Q2_Statutory_Homelessness_Detailed_Local_Authority_Data_202409_revised.ods` (registry, published 2026-02-24), supersedes edition 1.

- authorities changed: 190 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 5 |
| owed_duty | 5 |
| prevention_duty | 5 |
| relief_duty | 5 |
| households_in_ta | 14 |
| support_needs_total | 183 |

- households_in_ta: zero -> NULL 14, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 34, value -> value 183

### 2024Q3

Edition 2: `2024Q3_Statutory_Homelessness_Detailed_Local_Authority_Data_202412_revised.ods` (registry, published 2026-02-24), supersedes edition 1.

- authorities changed: 190 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 7 |
| owed_duty | 7 |
| prevention_duty | 7 |
| relief_duty | 7 |
| households_in_ta | 10 |
| support_needs_total | 181 |

- households_in_ta: zero -> NULL 10, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 38, value -> value 181

### 2024Q4

Edition 2: `2024Q4_Statutory_Homelessness_Detailed_Local_Authority_Data_202503_revised.ods` (registry, published 2026-02-24), supersedes edition 1.

- authorities changed: 191 of 296; no_change: False

| Measure | Cells changed |
|---|---|
| total_assessments | 2 |
| owed_duty | 2 |
| prevention_duty | 2 |
| relief_duty | 2 |
| households_in_ta | 11 |
| support_needs_total | 186 |

- households_in_ta: zero -> NULL 11, NULL -> zero 0, value -> value 0
- all measures: zero -> NULL 19, value -> value 186

